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
import re
import tempfile
from collections.abc import Iterable
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
from mcpforge.security.filters import CONTENT_PATTERNS
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


EVIDENCE_DIR_ENV = "MCPFORGE_E2E_EVIDENCE_DIR"
REDACTED = "[redacted]"
#: A whole PEM private key, not just its header line.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    """Every string in the structure, with known secret values and anything
    shaped like a credential replaced. Applied to the parsed report, not its
    JSON text, so an escaped character cannot hide a secret from the match."""
    if isinstance(value, dict):
        return {k: _redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        value = value.replace(secret, REDACTED)
    value = _PEM_BLOCK.sub(REDACTED, value)
    for _rule, pattern in CONTENT_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact(value: Any, secrets: Iterable[str | None]) -> Any:
    """The one redaction for everything a live leg writes to a file **or** to
    the job log — this repository is public, so an assert message is published.
    """
    # Short values are left alone: replacing a 3-character "secret" everywhere
    # would corrupt the evidence and protect nothing. Longest first, so a
    # secret containing another is removed whole.
    known = tuple(sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True))
    return _redact(value, known)


def save_validation_evidence(
    leg: str, detail: str, validation: dict[str, Any], secrets: Iterable[str | None]
) -> Path:
    """A failed leg's full validation report — every check's argv, exit code and
    stdout/stderr excerpts — written to a file, redacted, and its path printed.

    A paid run that fails must leave what failed behind: the first live run's
    typecheck and build failures were only diagnosable by paying for a
    reproduction. Written under `MCPFORGE_E2E_EVIDENCE_DIR` (the manual CI job
    uploads it) or a fresh temporary directory.
    """
    configured = os.environ.get(EVIDENCE_DIR_ENV)
    directory = Path(configured) if configured else Path(tempfile.mkdtemp(prefix="mcpforge-e2e-"))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{leg}-validation-evidence.json"
    body = redact({"leg": leg, "detail": detail, "validation": validation}, secrets)
    path.write_text(json.dumps(body, indent=1), encoding="utf-8")
    print(f"\n  [F9-01 {leg}] validation evidence saved to {path}")
    return path


def validation_failure_message(
    leg: str,
    detail: str,
    validation: dict[str, Any],
    secrets: Iterable[str | None],
    *,
    hint: str = "",
) -> str:
    """What a failed leg puts in the job log: the redacted summary and the
    evidence file's path — never the report itself, which goes to the file."""
    secrets = tuple(secrets)
    path = save_validation_evidence(leg, detail, validation, secrets)
    message = f"{detail}{hint} — full validation report (redacted) saved to {path}"
    return str(redact(message, secrets))


def test_a_failed_leg_saves_its_full_validation_evidence_without_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Always runs: the evidence path is not exercised only when money is spent."""
    monkeypatch.setenv(EVIDENCE_DIR_ENV, str(tmp_path / "evidence"))
    api_key = "live-gemini-key-value-0123456789"
    bearer = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJvd25lciJ9.c2lnbmF0dXJlLXZhbHVl"
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    tsc_error = "src/webmcp/tools/searchRooms.ts(56,38): error TS2322: Type 'string'"
    validation = {
        "completed": True,
        "report": {
            "checks": [
                {
                    "check_id": "typecheck",
                    "evidence": {
                        "exit_code": 2,
                        "stdout_excerpt": f"{tsc_error}\nkey={api_key}",
                        "stderr_excerpt": f"Authorization: Bearer {bearer}\n{pem}",
                    },
                }
            ]
        },
    }

    path = save_validation_evidence("analysis", "failed", validation, [api_key, bearer, None])

    assert path.parent == tmp_path / "evidence"
    text = path.read_text()
    for secret in (api_key, bearer, "MIIEow", "BEGIN RSA PRIVATE KEY"):
        assert secret not in text
    saved = json.loads(text)["validation"]["report"]["checks"][0]["evidence"]
    assert saved["exit_code"] == 2
    assert saved["stdout_excerpt"].startswith(tsc_error)
    assert REDACTED in saved["stderr_excerpt"]


def test_a_failed_legs_log_message_carries_no_secret_and_no_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The assert message is published in a public repository's job log. A
    secret planted in the report, or in the detail line, must not reach it."""
    monkeypatch.setenv(EVIDENCE_DIR_ENV, str(tmp_path))
    api_key = "live-gemini-key-value-0123456789"
    github_token = "ghp_" + "A" * 36
    validation = {
        "report": {
            "checks": [{"check_id": "build", "evidence": {"stderr_excerpt": f"k={api_key}"}}]
        }
    }

    message = validation_failure_message(
        "pr", f"typecheck failed {github_token}", validation, [api_key], hint=" — see lockfile"
    )

    assert api_key not in message and github_token not in message
    assert "stderr_excerpt" not in message, "the report itself went to the log"
    assert message.startswith(f"typecheck failed {REDACTED} — see lockfile")
    assert str(tmp_path / "pr-validation-evidence.json") in message


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
        bearer = make_token(subject=OWNER)
        d = Driver(client, bearer)
        secrets = (settings.gemini_api_key, settings.github_app_client_secret, bearer)
        session_id = d.create_session(d.create_project("live demo hotel"))

        assert d.step(session_id, "connect")["state"] == "ANALYSIS_PENDING"
        assert d.step(session_id, "analysis")["state"] == "WORKFLOW_SELECTION_PENDING"

        workflows = d.agent_read(session_id, "workflows")
        assert workflows["available"] is True
        chosen = _selectable(workflows["payload"])
        assert chosen, redact(
            f"the analyst found nothing a tool could call: {workflows['payload']}", secrets
        )
        print(f"\n  workflows selected: {chosen}")

        asked = d.agent(session_id, "workflows", {"workflow_ids": chosen})
        _undecided_is_refused(
            d, session_id, "workflows", {"approval_id": asked["approval_id"]}, 403
        )
        d.decide(asked["approval_id"])
        d.step(session_id, "workflows", {"approval_id": asked["approval_id"]})

        planned = d.step(session_id, "plan")
        assert planned["state"] == "TOOL_PLAN_APPROVAL_PENDING", redact(
            json.dumps(planned), secrets
        )
        plan_id = planned["approval"]["id"]
        _undecided_is_refused(d, session_id, "patch", {"approval_id": plan_id}, 403)
        _undecided_is_refused(d, session_id, "patch", None, 403)
        d.decide(plan_id)
        assert d.step(session_id, "patch", {"approval_id": plan_id})["state"] == "PATCH_READY"

        reviewed = d.step(session_id, "security-review")
        assert reviewed["state"] == "PATCH_APPROVAL_PENDING", redact(
            f"the live security review did not pass: {reviewed['detail']}", secrets
        )
        patch_id = reviewed["approval"]["id"]
        _undecided_is_refused(d, session_id, "validation", {"approval_id": patch_id}, 403)
        d.decide(patch_id)
        validated = d.step(session_id, "validation", {"approval_id": patch_id})
        report = d.agent_read(session_id, "validation")["payload"]
        if validated["state"] != "VALIDATION_PASSED":
            pytest.fail(
                validation_failure_message("analysis", validated["detail"], report, secrets)
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
        print(redact(f"  validation     : {validated['detail']}", secrets))
        print(f"  checks run     : {[c['check_id'] for c in executed]}")
        print(redact(f"  PR request     : refused — {refused['detail']}", secrets))
        print(redact(f"  PR writer      : refused — {writer_refusal}", secrets))


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
        bearer = make_token(subject=OWNER)
        d = Driver(client, bearer)
        secrets = (settings.gemini_api_key, settings.github_app_client_secret, bearer)
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
        assert reviewed["state"] == "PATCH_APPROVAL_PENDING", redact(reviewed["detail"], secrets)
        d.decide(reviewed["approval"]["id"])
        validated = d.step(session_id, "validation", {"approval_id": reviewed["approval"]["id"]})
        if validated["state"] != "VALIDATION_PASSED":
            pytest.fail(
                validation_failure_message(
                    "pr",
                    validated["detail"],
                    d.agent_read(session_id, "validation")["payload"],
                    secrets,
                    hint=(
                        " — the repository's dependencies come from the install step, which "
                        "needs a package-lock.json resolving only from registry.npmjs.org "
                        "(03_SECURITY_ACCESS.md §3)"
                    ),
                )
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
        assert done["state"] == "COMPLETE", redact(json.dumps(done), secrets)
        url = done["pull_request_url"]
        opened = [e for e in d.events(session_id) if e["label"] == "Pull request opened"]
        assert opened[0]["detail"]["branch"].startswith("mcpforge/webmcp-")
        print(redact(f"\n  pull request: {url}", secrets))
