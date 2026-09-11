"""F9-01 — the two live legs. Real Gemini, real sandbox, and for the PR leg,
real GitHub. They run **only on request**.

A full run calls Gemini several times and — for the PR leg — writes to GitHub,
so neither runs on every push:

- Without `MCPFORGE_E2E_LIVE=1` each leg **skips, and prints why**.
- With `MCPFORGE_E2E_LIVE_REQUIRED=1` a leg that cannot run **fails** instead of
  skipping. A skip that is green forever proves nothing; the manual CI job
  (`.github/workflows/e2e-live.yml`) sets both.

Nothing here is stubbed on the live path. The model is `GoogleGenAIProvider`
with the model id from `GEMINI_MODEL`; commands run in
`DevelopmentSecureExecutor`; the PR leg uses the real GitHub App installation.
The only test-only component is the identity — see `harness.py`: a real RS256
token checked by the real verifier, signed with the test session's key.

**Not a stand-in for anything.** `test_pipeline_offline.py` scripts the model,
the commands and GitHub and says so; it is not either of these legs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
import yaml  # type: ignore[import-untyped]
from fastapi.testclient import TestClient

from mcpforge.auth.firebase import FirebaseIdTokenVerifier
from mcpforge.config import Settings
from mcpforge.execution.development import DevelopmentSecureExecutor
from mcpforge.gemini.google_provider import GoogleGenAIProvider
from mcpforge.github.client import GitHubAppClient, GitHubError
from mcpforge.main import create_app
from mcpforge.store.memory import InMemoryStore
from tests.conftest import TEST_PROJECT, MakeToken, StubJWKClient
from tests.integration.harness import (
    DEMO_PATH,
    NODE_MODULES,
    PR_STATES,
    REPO_ROOT,
    Driver,
    assert_the_writer_refuses_a_demo_run,
    path_of,
)

LIVE_FLAG = "MCPFORGE_E2E_LIVE"
REQUIRED_FLAG = "MCPFORGE_E2E_LIVE_REQUIRED"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "e2e-live.yml"

#: The one repository the MCPForge GitHub App installation can reach.
TEST_REPOSITORY = "tony19053000/mcpforge-test"

OWNER = "e2e-live-developer"

#: Same reason as `test_validator.py`: Node reserves a large address range per
#: WebAssembly instance, and the executor's default RLIMIT_AS kills it.
VALIDATION_ADDRESS_SPACE_MB = 256 * 1024

#: `tests/conftest.py` clears these before every test so no unit test depends
#: on a developer's shell. The live legs need them, so they are read at import,
#: before any fixture runs, and put back only inside a live leg.
_LIVE_ENVIRONMENT = {
    name: os.environ[name]
    for name in (
        "GEMINI_BACKEND",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    )
    if name in os.environ
}


def skip_or_fail(reason: str) -> NoReturn:
    """Skip with the reason printed — or fail, when a live run was required."""
    if os.environ.get(REQUIRED_FLAG) == "1":
        pytest.fail(f"{REQUIRED_FLAG}=1, so this may not skip: {reason}")
    print(f"\n[F9-01 live leg skipped] {reason}")
    pytest.skip(reason)


def require_opt_in(leg: str) -> None:
    if os.environ.get(LIVE_FLAG) != "1":
        skip_or_fail(
            f"the {leg} calls Gemini (and GitHub) for real and runs only on request; "
            f"set {LIVE_FLAG}=1 to run it"
        )


def live_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name, value in _LIVE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=str(REPO_ROOT / ".env"))  # type: ignore[call-arg]


def require_toolchain() -> DevelopmentSecureExecutor:
    if not (NODE_MODULES / "vitest" / "vitest.mjs").is_file():
        skip_or_fail("node_modules/vitest is not installed; run npm ci at the repository root")
    for executable in ("/usr/bin/node", "/usr/bin/npm"):
        if not Path(executable).exists():
            skip_or_fail(f"{executable} is missing; the sandbox PATH cannot reach the toolchain")
    executor = DevelopmentSecureExecutor(memory_mb=VALIDATION_ADDRESS_SPACE_MB, cpu_seconds=1200)
    if not executor.network_isolation_available:
        skip_or_fail("this machine cannot deny a job the network, so the sandbox refuses to run")
    return executor


def _undecided_is_refused(
    d: Driver, session_id: str, stage: str, body: dict[str, Any] | None, expect: int
) -> None:
    """Try to pass a gate before the human decides. It must not move."""
    before = d.stored_state(session_id, OWNER)
    d.step(session_id, stage, body, expect=expect)
    assert d.stored_state(session_id, OWNER) == before, f"{stage} moved the run with no decision"


def _selectable(workflows_payload: dict[str, Any]) -> list[str]:
    """The workflows a developer would pick: confident, and implemented by an
    exported function in a service module, so a tool can call it."""
    index = workflows_payload["index"]
    callable_functions = {
        symbol["name"]
        for file in index["files"]
        if file["kind"] == "service"
        for symbol in file["symbols"]
        if symbol["exported"] and symbol["kind"] == "function"
    }
    return sorted(
        w["id"]
        for w in workflows_payload["analysis"]["workflows"]
        if w["confidence"] >= 0.6 and w["primary_function"] in callable_functions
    )


# ---------------------------------------------------------------------------
# The gating itself — these always run, and must be able to fail.
# ---------------------------------------------------------------------------


def test_a_live_leg_skips_with_its_reason_when_not_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LIVE_FLAG, raising=False)
    monkeypatch.delenv(REQUIRED_FLAG, raising=False)
    with pytest.raises(pytest.skip.Exception, match=LIVE_FLAG):
        require_opt_in("analysis leg")


def test_a_required_live_leg_fails_rather_than_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LIVE_FLAG, raising=False)
    monkeypatch.setenv(REQUIRED_FLAG, "1")
    with pytest.raises(pytest.fail.Exception, match=REQUIRED_FLAG):
        require_opt_in("analysis leg")


def test_the_live_job_is_manual_and_cannot_pass_by_skipping() -> None:
    """Read from the workflow file as YAML, where the properties live."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    # YAML 1.1 reads the bare key `on` as boolean true.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}, f"the live job must be manual only: {triggers}"

    steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
    runs = [s for s in steps if "tests/integration/test_e2e_live.py" in str(s.get("run", ""))]
    assert runs, "no step runs the live legs"
    for step in runs:
        assert step["env"][LIVE_FLAG] == "1"
        assert step["env"][REQUIRED_FLAG] == "1"
        # The step pipes pytest into `tee`. Without pipefail its status is
        # `tee`'s, so a failing leg left the job green (F9-01 review, round 2).
        # Asserted on the step that runs the legs, not anywhere in the file.
        assert step.get("shell") == "bash", "the live step must run under `shell: bash`"
        assert "set -o pipefail" in step["run"], "the live step must set pipefail"

    per_push = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    for job in per_push["jobs"].values():
        for step in job["steps"]:
            assert LIVE_FLAG not in (step.get("env") or {}), "the per-push CI opts into a paid run"


# ---------------------------------------------------------------------------
# The analysis leg — the bundled demo project, real Gemini, real sandbox.
# ---------------------------------------------------------------------------


def test_the_analysis_leg_runs_live_on_the_demo_project(
    monkeypatch: pytest.MonkeyPatch, jwks: StubJWKClient, make_token: MakeToken
) -> None:
    require_opt_in("analysis leg")
    settings = live_settings(monkeypatch)
    if not settings.gemini_configured:
        skip_or_fail("Gemini is not configured (GEMINI_API_KEY, or GEMINI_BACKEND=vertex)")
    executor = require_toolchain()

    app = create_app(
        settings,
        token_verifier=FirebaseIdTokenVerifier(TEST_PROJECT, jwks_client=jwks),  # type: ignore[arg-type]
        store=InMemoryStore(),
        gemini=GoogleGenAIProvider(settings),
        github=GitHubAppClient(app_id=None, private_key_path=None),
        executor=executor,
    )
    with TestClient(app) as client:
        d = Driver(client, make_token(subject=OWNER))
        session_id = d.create_session(d.create_project("live demo hotel"))

        assert d.step(session_id, "connect")["state"] == "ANALYSIS_PENDING"
        assert d.step(session_id, "analysis")["state"] == "WORKFLOW_SELECTION_PENDING"

        workflows = d.agent_read(session_id, "workflows")
        assert workflows["available"] is True
        chosen = _selectable(workflows["payload"])
        assert chosen, f"the analyst found nothing a tool could call: {workflows['payload']}"
        print(f"\n  workflows selected: {chosen}")

        asked = d.agent(session_id, "workflows", {"workflow_ids": chosen})
        _undecided_is_refused(
            d, session_id, "workflows", {"approval_id": asked["approval_id"]}, 403
        )
        d.decide(asked["approval_id"])
        d.step(session_id, "workflows", {"approval_id": asked["approval_id"]})

        planned = d.step(session_id, "plan")
        assert planned["state"] == "TOOL_PLAN_APPROVAL_PENDING", planned
        plan_id = planned["approval"]["id"]
        _undecided_is_refused(d, session_id, "patch", {"approval_id": plan_id}, 403)
        _undecided_is_refused(d, session_id, "patch", None, 403)
        d.decide(plan_id)
        assert d.step(session_id, "patch", {"approval_id": plan_id})["state"] == "PATCH_READY"

        reviewed = d.step(session_id, "security-review")
        assert reviewed["state"] == "PATCH_APPROVAL_PENDING", (
            f"the live security review did not pass: {reviewed['detail']}"
        )
        patch_id = reviewed["approval"]["id"]
        _undecided_is_refused(d, session_id, "validation", {"approval_id": patch_id}, 403)
        d.decide(patch_id)
        validated = d.step(session_id, "validation", {"approval_id": patch_id})
        report = d.agent_read(session_id, "validation")["payload"]
        assert validated["state"] == "VALIDATION_PASSED", (
            f"{validated['detail']}\n{json.dumps(report, indent=1)[:4000]}"
        )

        assert path_of(d.transitions(session_id)) == DEMO_PATH
        plan = d.agent_read(session_id, "plan")
        assert plan["available"] is True

        refused = d.step(session_id, "pull-request/request", expect=403)
        assert d.stored_state(session_id, OWNER) == "VALIDATION_PASSED"
        assert not {target for _, target in d.transitions(session_id)} & PR_STATES
        writer_refusal = assert_the_writer_refuses_a_demo_run(
            client, session_id, OWNER, TEST_REPOSITORY
        )

        executed = report["report"]["checks"]
        print(f"  tools          : {[t['name'] for t in plan['payload']['plan']['tools']]}")
        print(f"  validation     : {validated['detail']}")
        print(f"  checks run     : {[c['check_id'] for c in executed]}")
        print(f"  PR request     : refused — {refused['detail']}")
        print(f"  PR writer      : refused — {writer_refusal}")


# ---------------------------------------------------------------------------
# The PR leg — the dedicated GitHub test repository, real App installation.
# ---------------------------------------------------------------------------


async def _inspect_test_repository(settings: Settings) -> tuple[bool, dict[str, Any] | None]:
    """Read-only: is the repository granted, and what is its package.json?"""
    client = GitHubAppClient(
        app_id=settings.github_app_id, private_key_path=settings.github_app_private_key_path
    )
    try:
        for installation in await client.list_installations():
            token = await client.create_installation_token(installation.id)
            repositories = await client.list_repositories(token)
            if TEST_REPOSITORY not in {r.full_name for r in repositories}:
                continue
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.get(
                    f"https://api.github.com/repos/{TEST_REPOSITORY}/contents/package.json",
                    headers={
                        "Authorization": f"Bearer {token.token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
            if response.status_code == 404:
                return True, None
            response.raise_for_status()
            content = base64.b64decode(response.json()["content"]).decode()
            parsed: dict[str, Any] = json.loads(content)
            return True, parsed
    finally:
        await client.aclose()
    return False, None


def test_the_pr_leg_runs_live_on_the_test_repository(
    monkeypatch: pytest.MonkeyPatch, jwks: StubJWKClient, make_token: MakeToken
) -> None:
    require_opt_in("PR leg")
    settings = live_settings(monkeypatch)
    if not settings.github_configured:
        skip_or_fail(
            "the GitHub App is not configured (GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_PATH)"
        )
    if not settings.gemini_configured:
        skip_or_fail("Gemini is not configured")

    # Precondition, read-only, before anything is written anywhere.
    try:
        granted, package_json = asyncio.run(_inspect_test_repository(settings))
    except (GitHubError, httpx.HTTPError) as exc:
        pytest.fail(f"GitHub could not be read: {exc}")
    if not granted:
        pytest.fail(f"The App installation is not granted {TEST_REPOSITORY}.")
    dependencies = {
        **(package_json or {}).get("dependencies", {}),
        **(package_json or {}).get("devDependencies", {}),
    }
    if "next" not in dependencies:
        pytest.fail(
            f"{TEST_REPOSITORY} contains no Next.js application (no package.json declaring "
            "`next` on its default branch), so the pipeline has nothing it supports to "
            "analyze. This leg stops here and wrote nothing. Seeding the repository means "
            "writing to its default branch, which needs the project owner's decision "
            "(CLAUDE.md §6.1)."
        )

    executor = require_toolchain()
    app = create_app(
        settings,
        token_verifier=FirebaseIdTokenVerifier(TEST_PROJECT, jwks_client=jwks),  # type: ignore[arg-type]
        store=InMemoryStore(),
        gemini=GoogleGenAIProvider(settings),
        executor=executor,
    )
    with TestClient(app) as client:
        d = Driver(client, make_token(subject=OWNER))
        project_id = d.create_project("live test repository")
        session_id = d.create_session(project_id)

        asked = d.agent(
            session_id, "repository", {"repository_full_name": TEST_REPOSITORY, "branch": "main"}
        )
        _undecided_is_refused(
            d, session_id, "connect", {"repository_binding_approval_id": asked["approval_id"]}, 403
        )
        d.decide(asked["approval_id"])
        connected = d.step(
            session_id, "connect", {"repository_binding_approval_id": asked["approval_id"]}
        )
        assert connected["state"] == "ANALYSIS_PENDING"
        d.step(session_id, "analysis")

        chosen = _selectable(d.agent_read(session_id, "workflows")["payload"])
        assert chosen, "the analyst found nothing a tool could call"
        d.step(session_id, "workflows", {"workflow_ids": chosen})
        plan_id = d.step(session_id, "plan")["approval"]["id"]
        d.decide(plan_id)
        d.step(session_id, "patch", {"approval_id": plan_id})
        reviewed = d.step(session_id, "security-review")
        assert reviewed["state"] == "PATCH_APPROVAL_PENDING", reviewed["detail"]
        d.decide(reviewed["approval"]["id"])
        validated = d.step(session_id, "validation", {"approval_id": reviewed["approval"]["id"]})
        assert validated["state"] == "VALIDATION_PASSED", (
            f"{validated['detail']} — the repository's dependencies come from the install "
            "step, which needs a package-lock.json resolving only from registry.npmjs.org "
            "(03_SECURITY_ACCESS.md §3)"
        )

        # The explicit, recorded elevation. Before it, the gate cannot open.
        d.step(session_id, "pull-request/request", expect=403)
        elevated = d.post(f"/api/projects/{project_id}/access/elevate").json()
        assert elevated["access_mode"] == "WRITE_PR" and elevated["elevated_by"] == OWNER

        requested = d.step(session_id, "pull-request/request")
        pr_id = requested["approval"]["id"]
        _undecided_is_refused(d, session_id, "pull-request", {"approval_id": pr_id}, 403)
        d.decide(pr_id)
        done = d.step(session_id, "pull-request", {"approval_id": pr_id})
        assert done["state"] == "COMPLETE", done
        url = done["pull_request_url"]
        opened = [e for e in d.events(session_id) if e["label"] == "Pull request opened"]
        assert opened[0]["detail"]["branch"].startswith("mcpforge/webmcp-")
        print(f"\n  pull request: {url}")
