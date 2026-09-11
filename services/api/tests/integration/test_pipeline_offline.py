"""F9-01 — the pipeline composed end to end, offline. Every gate, every edge.

**What is scripted here, stated plainly.** This file is not the live
end-to-end run and cannot stand in for it; `test_e2e_live.py` is that. Three
things are stand-ins, each named for what it is:

- `ScriptedGemini` — canned responses, re-validated against the schema each
  agent asks for. **Not Gemini.** No test here says anything about model
  quality.
- `ScriptedCommandExecutor` — real ephemeral directories, **scripted exit
  codes**. Not validation evidence and not isolation. Its "clone" copies the
  demo fixture.
- `FakeGitHub` — an `httpx.MockTransport` recording every request. **Not
  GitHub.**

What is real: every route; the verifier checking an RS256 token signed with the
session's test key; the store; `RunMachine` and every gate it checks; the
indexer, filter and retriever over the real demo fixture; the plan-to-toolset
conversion; the generator; the policy engine and `evaluate_gate`; the
validator's planning, liveness guard and scoring; and the pull-request writer's
refusals and exact request sequence. Every approval is decided through
`POST /api/approvals/{id}/decide`.

That is what this suite is for: that the stages compose, that **every edge in
the transition table is driven** (`test_every_transition_in_the_table_is_driven`),
and that **no run completes with any approval skipped**, each skip attempted in
turn (`test_the_analysis_leg_cannot_complete_with_an_approval_skipped`,
`test_the_pr_leg_cannot_complete_with_an_approval_skipped`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from collections.abc import AsyncGenerator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mcpforge.agents.validator import VITEST_CLI
from mcpforge.auth.firebase import FirebaseIdTokenVerifier
from mcpforge.config import Settings
from mcpforge.execution.provider import (
    AttestationEvidence,
    Command,
    CommandResult,
    SandboxError,
    TrustLevel,
    Workspace,
    WorkspaceSpec,
)
from mcpforge.gemini.provider import (
    GeminiSchemaError,
    GeminiTransportError,
    GenerationRequest,
    SchemaT,
)
from mcpforge.github.client import GitHubAppClient
from mcpforge.main import create_app
from mcpforge.models.core import ApprovalStatus, Artifact, ArtifactKind
from mcpforge.orchestration.dependencies import install_command
from mcpforge.orchestration.pipeline import PipelineOptions
from mcpforge.store.memory import InMemoryStore
from tests.conftest import TEST_PROJECT, MakeToken, StubJWKClient
from tests.integration.harness import (
    ALL_TRANSITIONS,
    DEMO_APP,
    DEMO_PATH,
    PR_STATES,
    Driver,
    assert_the_writer_refuses_a_demo_run,
    path_of,
)

OWNER = "e2e-developer"
REPO = "acme/hotel"
REPO_ID = 4242
BASE_SHA = "b" * 40
INSTALLATION_ID = 7

WORKFLOW_IDS = ["cancel_reservation", "check_availability", "search_rooms"]

# ---------------------------------------------------------------------------
# Scripted model responses — shaped for the real demo fixture, so the analyst's
# and architect's own evidence checks run against the real index and pass.
# ---------------------------------------------------------------------------

ANALYSIS: dict[str, Any] = {
    "framework": "next.js",
    "summary": "A hotel booking application.",
    "business_operations": [
        {
            "name": "searchRooms",
            "summary": "Filters the room inventory.",
            "risk": "READ",
            "evidence": {"path": "src/lib/rooms.ts", "symbol": "searchRooms"},
        }
    ],
    "workflows": [
        {
            "id": "search_rooms",
            "name": "Search rooms",
            "description": "Find rooms for a guest count and budget.",
            "risk": "READ",
            "primary_function": "searchRooms",
            "evidence": [{"path": "src/lib/rooms.ts", "symbol": "searchRooms"}],
            "confidence": 0.9,
        },
        {
            "id": "check_availability",
            "name": "Check availability",
            "description": "Check whether a room is free.",
            "risk": "READ",
            "primary_function": "checkAvailability",
            "evidence": [{"path": "src/lib/availability.ts", "symbol": "checkAvailability"}],
            "confidence": 0.9,
        },
        {
            "id": "cancel_reservation",
            "name": "Cancel a reservation",
            "description": "Cancel an existing booking.",
            "risk": "DESTRUCTIVE",
            "primary_function": "cancelReservation",
            "evidence": [{"path": "src/lib/reservations.ts", "symbol": "cancelReservation"}],
            "confidence": 0.85,
        },
    ],
    "unknowns": [],
}


def _tool(
    name: str, function: str, path: str, params: list[dict[str, Any]], risk: str, **extra: Any
) -> dict[str, Any]:
    return {
        "name": name,
        "title": name.replace("_", " ").capitalize(),
        "description": extra.get("description", f"{name.replace('_', ' ').capitalize()}."),
        "workflow_id": name,
        "maps_to_function": function,
        "parameters": params,
        "output_description": "The result.",
        "risk": risk,
        "evidence": [{"path": path, "symbol": function}],
    }


def _plan(*, search_description: str | None = None) -> dict[str, Any]:
    search = _tool(
        "search_rooms",
        "searchRooms",
        "src/lib/rooms.ts",
        [
            {"name": "guests", "json_type": "integer", "description": "Guests"},
            {"name": "maxPrice", "json_type": "number", "description": "Budget", "required": False},
        ],
        "READ",
    )
    if search_description is not None:
        search["description"] = search_description
    return {
        "tools": [
            search,
            _tool(
                "check_availability",
                "checkAvailability",
                "src/lib/availability.ts",
                [
                    {"name": "roomId", "json_type": "string", "description": "Room"},
                    {"name": "checkIn", "json_type": "string", "description": "ISO date"},
                    {"name": "checkOut", "json_type": "string", "description": "ISO date"},
                ],
                "READ",
            ),
            _tool(
                "cancel_reservation",
                "cancelReservation",
                "src/lib/reservations.ts",
                [{"name": "reservationId", "json_type": "string", "description": "Booking"}],
                "DESTRUCTIVE",
            ),
        ],
        "notes": [],
    }


PLAN = _plan()
#: A plan naming a function the index does not have. The architect's own
#: evidence check rejects it, every attempt.
BAD_PLAN: dict[str, Any] = {
    "tools": [_tool("wipe_everything", "wipeEverything", "src/lib/rooms.ts", [], "READ")],
    "notes": [],
}
#: Assembled at runtime so no credential-shaped literal is ever committed.
PLANTED_KEY = "AKIA" + "Q" * 16
SECRET_PLAN = _plan(search_description=f"Search rooms. Internal key {PLANTED_KEY}.")

REVIEW_PASS: dict[str, Any] = {"advisory_pass": True, "findings": [], "summary": "No issues."}
REVIEW_BLOCK: dict[str, Any] = {
    "advisory_pass": False,
    "findings": [
        {
            "rule": "agent.unbounded-cancellation",
            "severity": "HIGH",
            "summary": "Scripted blocking finding.",
            "recommendation": "Scripted.",
        }
    ],
    "summary": "Blocked.",
}


class ScriptedGemini:
    """TEST ONLY. Canned responses by requested schema. **This is not Gemini.**

    Every response is re-validated against the schema the agent asked for, so
    a scripted response the real provider would reject is rejected here too.
    """

    def __init__(self) -> None:
        self.defaults: dict[str, dict[str, Any]] = {
            "CodebaseAnalysis": ANALYSIS,
            "ProposedToolPlan": PLAN,
            "SecurityReport": REVIEW_PASS,
        }
        self.queues: dict[str, list[dict[str, Any] | Exception]] = {}

    @property
    def configured(self) -> bool:
        return True

    @property
    def model(self) -> str:
        return "scripted-responses-not-gemini"

    def queue(self, schema: str, *items: dict[str, Any] | Exception) -> None:
        self.queues.setdefault(schema, []).extend(items)

    async def generate_structured(
        self, request: GenerationRequest, schema: type[SchemaT]
    ) -> SchemaT:
        queued = self.queues.get(schema.__name__)
        item = queued.pop(0) if queued else self.defaults[schema.__name__]
        if isinstance(item, Exception):
            raise item
        try:
            return schema.model_validate(item)
        except ValidationError as exc:
            raise GeminiSchemaError(f"scripted response invalid: {exc}") from exc

    def stream_text(self, request: GenerationRequest) -> AsyncGenerator[str]:
        raise AssertionError("the pipeline never streams text")


#: STAND-IN: the lockfile the scripted repository carries, shaped like a real
#: `package-lock.json` v3 resolving only from the npm registry. The integrity
#: values are placeholders; nothing is fetched against them.
SCRIPTED_LOCKFILE: dict[str, Any] = {
    "name": "demo-hotel-app",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {
        "": {"name": "demo-hotel-app", "version": "0.1.0"},
        "node_modules/next": {
            "version": "16.3.4",
            "resolved": "https://registry.npmjs.org/next/-/next-16.3.4.tgz",
            "integrity": "sha512-placeholderForNext",
        },
        "node_modules/vitest": {
            "version": "3.2.0",
            "resolved": "https://registry.npmjs.org/vitest/-/vitest-3.2.0.tgz",
            "integrity": "sha512-placeholderForVitest",
        },
    },
}


def _lockfile_bytes(lock: dict[str, Any]) -> bytes:
    return json.dumps(lock, indent=2).encode()


class ScriptedCommandExecutor:
    """TEST ONLY. Real ephemeral directories; **scripted** command results.

    Not validation evidence and not isolation — the live leg uses
    `DevelopmentSecureExecutor`. It does enforce the one property this suite
    needs from a workspace: a clone only in a networked workspace, every other
    command only in one without the network.
    """

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self._root = root
        self.fail_vitest = False
        self.refuse_workspaces = False
        #: The lockfile the scripted "clone" carries. `None` is a repository
        #: without one.
        self.lockfile: dict[str, Any] | None = SCRIPTED_LOCKFILE
        #: A repository that commits its own `node_modules`. The pipeline must
        #: never use it.
        self.ship_node_modules = False
        #: STAND-IN FOR THE REGISTRY: `npm ci` is answered here, and "installs"
        #: by writing a placeholder tree. Nothing is downloaded, and nothing in
        #: the tree is ever executed — the commands that would use it are
        #: scripted too.
        self.install_exit_code = 0
        self.install_provides_vitest = True
        #: Every command, with whether its workspace had the network.
        self.runs: list[tuple[tuple[str, ...], bool]] = []

    @property
    def trust_level(self) -> TrustLevel:
        return TrustLevel.DEVELOPMENT_ISOLATION

    async def create_workspace(self, spec: WorkspaceSpec) -> Workspace:
        if self.refuse_workspaces:
            raise SandboxError("scripted: this executor refuses to create a workspace")
        path = Path(tempfile.mkdtemp(prefix=f"{spec.run_id}-", dir=self._root)).resolve()  # noqa: ASYNC240 - a test double's temporary directory
        return Workspace(
            id=path.name, root=path, trust_level=self.trust_level, allow_network=spec.allow_network
        )

    async def run(self, workspace: Workspace, command: Command) -> CommandResult:
        argv = command.argv
        self.runs.append((argv, workspace.allow_network))

        def done(code: int = 0, stdout: str = "") -> CommandResult:
            return CommandResult(
                argv=argv, exit_code=code, stdout=stdout, stderr="", duration_seconds=0.0
            )

        if argv[:2] == ("git", "clone"):
            assert workspace.allow_network, "a clone ran in a workspace with no network"
            shutil.copytree(
                DEMO_APP,
                workspace.root / argv[-1],
                ignore=shutil.ignore_patterns("node_modules", ".next"),
            )
            checkout = workspace.root / argv[-1]
            if self.lockfile is not None:
                (checkout / "package-lock.json").write_bytes(_lockfile_bytes(self.lockfile))
            if self.ship_node_modules:
                shipped = checkout / "node_modules" / "vitest"
                shipped.mkdir(parents=True)
                (shipped / "vitest.mjs").write_text("// shipped by the repository\n")
            return done()
        if argv[:3] == ("git", "rev-parse", "HEAD"):
            return done(stdout=BASE_SHA + "\n")
        if argv[:2] == ("npm", "ci"):
            assert workspace.allow_network, "the install ran in a workspace with no network"
            if self.install_exit_code:
                return done(self.install_exit_code, "npm ERR! scripted install failure")
            tree = workspace.root / (command.cwd or ".") / "node_modules"
            package = tree / ("vitest" if self.install_provides_vitest else "left-pad")
            package.mkdir(parents=True)
            entry = "vitest.mjs" if self.install_provides_vitest else "index.js"
            (package / entry).write_text("// stand-in for a registry download; never executed\n")
            return done()
        assert not workspace.allow_network, f"{argv} ran in a networked workspace"
        if argv[:2] == ("node", VITEST_CLI):
            if self.fail_vitest:
                return done(1, "      Tests  1 failed (1)\n")
            return done(0, "      Tests  1 passed (1)\n")
        if argv[:2] == ("npm", "run"):
            return done()
        raise AssertionError(f"unexpected command: {argv}")

    async def attestation(self) -> AttestationEvidence | None:
        return None

    async def destroy(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.root, ignore_errors=True)


class FakeGitHub:
    """TEST ONLY. A recorded stand-in for the GitHub REST API. **Not GitHub.**"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_next_pull = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        self.calls.append((method, path, body))
        repo = f"/repos/{REPO}"

        if (method, path) == ("GET", "/app/installations"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": INSTALLATION_ID,
                        "account": {"login": "acme"},
                        "repository_selection": "selected",
                    }
                ],
            )
        if (method, path) == ("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens"):
            return httpx.Response(
                201,
                json={
                    "token": "installation-token-for-tests",
                    "expires_at": "2999-01-01T00:00:00Z",
                },
            )
        if (method, path) == ("GET", "/installation/repositories"):
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": REPO_ID,
                            "full_name": REPO,
                            "default_branch": "main",
                            "private": True,
                        }
                    ]
                },
            )
        if (method, path) == ("GET", f"{repo}/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": BASE_SHA}})
        if method == "GET" and path.startswith(f"{repo}/git/ref/heads/mcpforge/"):
            return httpx.Response(404, json={"message": "Not Found"})
        if (method, path) == ("GET", f"{repo}/git/commits/{BASE_SHA}"):
            return httpx.Response(200, json={"tree": {"sha": "treesha"}})
        if (method, path) == ("POST", f"{repo}/git/blobs"):
            return httpx.Response(201, json={"sha": "blobsha"})
        if (method, path) == ("POST", f"{repo}/git/trees"):
            return httpx.Response(201, json={"sha": "newtree"})
        if (method, path) == ("POST", f"{repo}/git/commits"):
            return httpx.Response(201, json={"sha": "newcommit"})
        if (method, path) == ("POST", f"{repo}/git/refs"):
            return httpx.Response(201, json={"ref": body.get("ref")})
        if (method, path) == ("POST", f"{repo}/pulls"):
            if self.fail_next_pull:
                self.fail_next_pull = False
                return httpx.Response(500, json={"message": "scripted outage"})
            return httpx.Response(
                201, json={"number": 12, "html_url": f"https://github.test/{REPO}/pull/12"}
            )
        if method == "DELETE" and path.startswith(f"{repo}/git/refs/heads/mcpforge/"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected GitHub call: {method} {path}")


@dataclass
class Rig:
    driver: Driver
    gemini: ScriptedGemini
    executor: ScriptedCommandExecutor
    github: FakeGitHub

    @property
    def store(self) -> InMemoryStore:
        store: InMemoryStore = self.driver.client.app.state.store  # type: ignore[attr-defined]
        return store


@pytest.fixture
def rig(
    tmp_path: Path,
    settings: Settings,
    jwks: StubJWKClient,
    make_token: MakeToken,
    private_pem: str,
) -> Iterator[Rig]:
    key = tmp_path / "app.pem"
    key.write_text(private_pem)
    github = FakeGitHub()
    # A dependency tree holding only the file the validator looks for. The
    # commands that would use it are scripted; nothing ever executes it.
    scripted_tree = tmp_path / "scripted-node-modules"
    (scripted_tree / "vitest").mkdir(parents=True)
    (scripted_tree / "vitest" / "vitest.mjs").write_text("// scripted; never executed\n")

    gemini = ScriptedGemini()
    executor = ScriptedCommandExecutor(tmp_path / "workspaces")
    app = create_app(
        settings,
        token_verifier=FirebaseIdTokenVerifier(TEST_PROJECT, jwks_client=jwks),  # type: ignore[arg-type]
        store=InMemoryStore(),
        gemini=gemini,
        github=GitHubAppClient(
            app_id="12345",
            private_key_path=str(key),
            http=httpx.AsyncClient(transport=github.transport()),
        ),
        executor=executor,
        pipeline_options=PipelineOptions(
            demo_fixture=DEMO_APP,
            demo_dependency_tree=scripted_tree,
            writer_transport=github.transport(),
        ),
    )
    with TestClient(app) as client:
        yield Rig(Driver(client, make_token(subject=OWNER)), gemini, executor, github)


# ---------------------------------------------------------------------------
# Gates, and what skipping one means
# ---------------------------------------------------------------------------


class GateSkippedError(Exception):
    def __init__(self, gate: str, session_id: str) -> None:
        super().__init__(gate)
        self.gate = gate
        self.session_id = session_id


def through_gate(
    rig: Rig,
    session_id: str,
    gate: str,
    approval_id: str,
    proceed: Callable[[str | None, int], dict[str, Any]],
    *,
    skip: str | None,
    missing_status: int | None,
) -> dict[str, Any]:
    """Pass a gate the way the developer does — or, when `skip` names it, try
    every way of passing it without a decision, and stop.

    Skipping means: the approval exists and is PENDING, and the run is asked to
    continue anyway — with the undecided approval, and with no approval at all
    where the route allows the field to be absent. Each must be refused, the
    stored state must not move, and the approval must still be PENDING.
    """
    d = rig.driver
    if skip != gate:
        d.decide(approval_id)
        return proceed(approval_id, 200)

    before = d.stored_state(session_id, OWNER)
    proceed(approval_id, 403)
    if missing_status is not None:
        proceed(None, missing_status)
    assert d.stored_state(session_id, OWNER) == before, f"skipping {gate} moved the run"
    stored = asyncio.run(rig.store.get_approval(approval_id, OWNER))
    assert stored.status is ApprovalStatus.PENDING and stored.actor_uid is None
    raise GateSkippedError(gate, session_id)


def _stage(
    rig: Rig, session_id: str, stage: str, field: str
) -> Callable[[str | None, int], dict[str, Any]]:
    def proceed(approval_id: str | None, expect: int) -> dict[str, Any]:
        body = {field: approval_id} if approval_id is not None else None
        return rig.driver.step(session_id, stage, body, expect=expect)

    return proceed


def to_workflows_selected(rig: Rig, session_id: str, skip: str | None) -> None:
    d = rig.driver
    assert d.step(session_id, "analysis")["state"] == "WORKFLOW_SELECTION_PENDING"
    asked = d.agent(session_id, "workflows", {"workflow_ids": WORKFLOW_IDS})
    selected = through_gate(
        rig,
        session_id,
        "WORKFLOW_SELECTION",
        asked["approval_id"],
        _stage(rig, session_id, "workflows", "approval_id"),
        skip=skip,
        # The developer selecting directly is not a skip: the click is the
        # decision (02_ARCHITECTURE.md §10.2). So there is no "absent" probe.
        missing_status=None,
    )
    assert selected["state"] == "WORKFLOWS_SELECTED"


def plan_to_validation(rig: Rig, session_id: str, skip: str | None) -> dict[str, Any]:
    d = rig.driver
    planned = d.step(session_id, "plan")
    assert planned["state"] == "TOOL_PLAN_APPROVAL_PENDING"
    generated = through_gate(
        rig,
        session_id,
        "TOOL_PLAN",
        planned["approval"]["id"],
        _stage(rig, session_id, "patch", "approval_id"),
        skip=skip,
        missing_status=403,
    )
    assert generated["state"] == "PATCH_READY"
    reviewed = d.step(session_id, "security-review")
    assert reviewed["state"] == "PATCH_APPROVAL_PENDING"
    validated = through_gate(
        rig,
        session_id,
        "PATCH",
        reviewed["approval"]["id"],
        _stage(rig, session_id, "validation", "approval_id"),
        skip=skip,
        missing_status=422,
    )
    return validated


def run_analysis_leg(rig: Rig, *, skip: str | None = None) -> str:
    d = rig.driver
    session_id = d.create_session(d.create_project("demo hotel"))
    assert d.step(session_id, "connect")["state"] == "ANALYSIS_PENDING"
    to_workflows_selected(rig, session_id, skip)
    validated = plan_to_validation(rig, session_id, skip)
    assert validated["state"] == "VALIDATION_PASSED", validated
    return session_id


def run_pr_leg(rig: Rig, *, skip: str | None = None, elevate: bool = True) -> str:
    d = rig.driver
    project_id = d.create_project("hotel on github")
    session_id = d.create_session(project_id)
    asked = d.agent(session_id, "repository", {"repository_full_name": REPO, "branch": "main"})
    connected = through_gate(
        rig,
        session_id,
        "REPOSITORY_BINDING",
        asked["approval_id"],
        _stage(rig, session_id, "connect", "repository_binding_approval_id"),
        skip=skip,
        missing_status=None,
    )
    assert connected["state"] == "ANALYSIS_PENDING"
    to_workflows_selected(rig, session_id, skip)
    assert plan_to_validation(rig, session_id, skip)["state"] == "VALIDATION_PASSED"

    if elevate:
        elevated = d.post(f"/api/projects/{project_id}/access/elevate")
        assert elevated.status_code == 200, elevated.text
        assert elevated.json()["elevated_by"] == OWNER

    requested = d.step(session_id, "pull-request/request")
    assert requested["state"] == "PR_APPROVAL_PENDING"
    done = through_gate(
        rig,
        session_id,
        "PULL_REQUEST",
        requested["approval"]["id"],
        _stage(rig, session_id, "pull-request", "approval_id"),
        skip=skip,
        missing_status=403,
    )
    assert done["state"] == "COMPLETE", done
    return session_id


# ---------------------------------------------------------------------------
# The analysis leg
# ---------------------------------------------------------------------------


def test_the_analysis_leg_reaches_validation_passed_through_every_gate(rig: Rig) -> None:
    d = rig.driver
    session_id = run_analysis_leg(rig)

    assert path_of(d.transitions(session_id)) == DEMO_PATH
    assert d.stored_state(session_id, OWNER) == "VALIDATION_PASSED"

    # The F7-02 read tools now answer from real stage output.
    for read in ("workflows", "plan", "validation"):
        body = d.agent_read(session_id, read)
        assert body["available"] is True, read
    report = d.agent_read(session_id, "validation")["payload"]["report"]
    assert report["checks"], "validation passed with no executed check"

    # Every decision on this run was a human's, through the decide endpoint.
    decided = [e for e in d.events(session_id) if e["kind"] == "approval.decided"]
    assert len(decided) == 3
    assert {e["origin"] for e in decided} == {"HUMAN"}
    for event in decided:
        approval = d.get(f"/api/approvals/{event['detail']['approval_id']}").json()
        assert approval["actor_uid"] == OWNER


def test_the_analysis_leg_cannot_reach_a_pull_request_state(rig: Rig) -> None:
    d = rig.driver
    session_id = run_analysis_leg(rig)

    refused = d.step(session_id, "pull-request/request", expect=403)
    assert "no bound repository" in refused["detail"]
    refused = d.step(session_id, "pull-request", {"approval_id": "appr_x"}, expect=409)
    assert d.stored_state(session_id, OWNER) == "VALIDATION_PASSED"
    assert not {target for _, target in d.transitions(session_id)} & PR_STATES

    # And the demo project cannot be elevated to try again.
    project_id = d.agent_read(session_id, "status")["project_id"]
    assert d.post(f"/api/projects/{project_id}/access/elevate").status_code == 409


def test_the_pr_writer_refuses_the_analysis_leg(rig: Rig) -> None:
    session_id = run_analysis_leg(rig)
    reason = assert_the_writer_refuses_a_demo_run(rig.driver.client, session_id, OWNER, REPO)
    assert "no bound repository" in reason


@pytest.mark.parametrize("gate", ["WORKFLOW_SELECTION", "TOOL_PLAN", "PATCH"])
def test_the_analysis_leg_cannot_complete_with_an_approval_skipped(rig: Rig, gate: str) -> None:
    with pytest.raises(GateSkippedError) as skipped:
        run_analysis_leg(rig, skip=gate)
    assert skipped.value.gate == gate
    visited = path_of(rig.driver.transitions(skipped.value.session_id))
    assert "VALIDATION_PASSED" not in visited
    assert rig.driver.stored_state(skipped.value.session_id, OWNER) != "VALIDATION_PASSED"


# ---------------------------------------------------------------------------
# The PR leg, against a recorded GitHub
# ---------------------------------------------------------------------------


def test_the_pr_leg_opens_a_pull_request_on_an_mcpforge_branch_only(rig: Rig) -> None:
    d = rig.driver
    session_id = run_pr_leg(rig)

    visited = path_of(d.transitions(session_id))
    assert visited == (
        "PROJECT_CREATED",
        "REPOSITORY_CONNECTED",
        *DEMO_PATH[1:],
        "PR_APPROVAL_PENDING",
        "PR_APPROVED",
        "PR_CREATING",
        "PR_CREATED",
        "COMPLETE",
    )

    writes = [(m, p, b) for m, p, b in rig.github.calls if m in ("POST", "PATCH", "PUT", "DELETE")]
    refs = [b for m, p, b in writes if p.endswith("/git/refs")]
    assert [r["ref"] for r in refs] == [
        f"refs/heads/mcpforge/webmcp-{session_id.replace('_', '-')}"
    ]
    pulls = [b for m, p, b in writes if p.endswith("/pulls")]
    assert len(pulls) == 1
    assert pulls[0]["base"] == "main"
    assert pulls[0]["head"].startswith("mcpforge/webmcp-")
    # Nothing touched the default branch, and nothing could update a ref.
    assert not [c for c in writes if "heads/main" in c[1]]
    assert not [c for c in writes if c[0] in ("PATCH", "PUT")]
    # The base the writer built on is the one GitHub reported, not self-reported.
    commits = [b for m, p, b in writes if p.endswith("/git/commits")]
    assert commits[0]["parents"] == [BASE_SHA]

    opened = [e for e in d.events(session_id) if e["label"] == "Pull request opened"]
    assert opened and opened[0]["detail"]["url"].endswith("/pull/12")


@pytest.mark.parametrize(
    "gate", ["REPOSITORY_BINDING", "WORKFLOW_SELECTION", "TOOL_PLAN", "PATCH", "PULL_REQUEST"]
)
def test_the_pr_leg_cannot_complete_with_an_approval_skipped(rig: Rig, gate: str) -> None:
    with pytest.raises(GateSkippedError) as skipped:
        run_pr_leg(rig, skip=gate)
    visited = path_of(rig.driver.transitions(skipped.value.session_id))
    assert "PR_CREATED" not in visited and "COMPLETE" not in visited
    assert not [c for c in rig.github.calls if c[0] == "POST" and c[1].endswith("/pulls")]


def test_the_pr_leg_cannot_open_a_pull_request_without_the_recorded_elevation(rig: Rig) -> None:
    with pytest.raises(AssertionError, match="pull-request/request: 403"):
        run_pr_leg(rig, elevate=False)
    # Minting an installation token is a POST that writes nothing; the property
    # is that nothing was written to the repository.
    repository_writes = [
        c for c in rig.github.calls if c[0] != "GET" and c[1].startswith(f"/repos/{REPO}/")
    ]
    assert not repository_writes, repository_writes


def test_an_approval_from_another_run_does_not_open_this_gate(rig: Rig) -> None:
    """Artifact hashes are derived from content, so two runs over the same
    fixture produce the same plan hash. The approval must still not travel."""
    d = rig.driver
    first = d.create_session(d.create_project("first"))
    second = d.create_session(d.create_project("second"))
    approvals = {}
    for session_id in (first, second):
        d.step(session_id, "connect")
        d.step(session_id, "analysis")
        d.step(session_id, "workflows", {"workflow_ids": WORKFLOW_IDS})
        approvals[session_id] = d.step(session_id, "plan")["approval"]
    assert approvals[first]["artifact_hash"] == approvals[second]["artifact_hash"]

    d.decide(approvals[first]["id"])
    d.step(second, "patch", {"approval_id": approvals[first]["id"]}, expect=403)
    assert d.stored_state(second, OWNER) == "TOOL_PLAN_APPROVAL_PENDING"


def test_an_agent_cannot_move_the_pipeline_itself(rig: Rig) -> None:
    """The agent stage endpoints still start nothing (`02_ARCHITECTURE.md`
    §10.1). A run driven only by an agent stays where it began."""
    d = rig.driver
    session_id = d.create_session(d.create_project("agent only"))
    for path, body in (("analysis", None), ("security-review", None), ("validation", None)):
        response = d.agent(session_id, path, body)
        assert response["started"] is False
    assert d.stored_state(session_id, OWNER) == "PROJECT_CREATED"
    assert d.transitions(session_id) == []


def _snake_case_plan() -> dict[str, Any]:
    """What the first live run's model actually proposed for `checkAvailability`,
    which is declared `(roomId, checkIn, checkOut)`."""
    plan = _plan()
    plan["tools"][1]["parameters"] = [
        {"name": "room_id", "json_type": "string", "description": "Room"},
        {"name": "check_in", "json_type": "string", "description": "ISO date"},
        {"name": "check_out", "json_type": "string", "description": "ISO date"},
    ]
    return plan


def _to_workflows_selected_directly(rig: Rig) -> str:
    d = rig.driver
    session_id = d.create_session(d.create_project("binding"))
    d.step(session_id, "connect")
    d.step(session_id, "analysis")
    d.step(session_id, "workflows", {"workflow_ids": WORKFLOW_IDS})
    return session_id


def test_an_unbindable_plan_costs_the_model_a_retry_not_the_stage(rig: Rig) -> None:
    """The architect's own verification runs the pipeline's binding, so the
    model is asked again — and the plan stored is the one that binds."""
    d = rig.driver
    session_id = _to_workflows_selected_directly(rig)
    rig.gemini.queue("ProposedToolPlan", _snake_case_plan())

    planned = d.step(session_id, "plan")
    assert planned["state"] == "TOOL_PLAN_APPROVAL_PENDING"
    stored = d.agent_read(session_id, "plan")["payload"]["plan"]
    availability = next(t for t in stored["tools"] if t["name"] == "check_availability")
    assert [p["name"] for p in availability["parameters"]] == ["roomId", "checkIn", "checkOut"]
    assert not rig.gemini.queues["ProposedToolPlan"], "the unbindable proposal was not consumed"


def test_a_plan_that_never_binds_fails_the_stage_and_asks_no_one(rig: Rig) -> None:
    d = rig.driver
    session_id = _to_workflows_selected_directly(rig)
    rig.gemini.queue("ProposedToolPlan", *[_snake_case_plan()] * 3)

    refused = d.step(session_id, "plan", expect=409)
    assert "room_id" in refused["detail"]
    assert d.stored_state(session_id, OWNER) == "TOOL_PLAN_RUNNING"
    assert d.agent_read(session_id, "plan")["available"] is False
    assert not [e for e in d.events(session_id) if e["kind"] == "approval.requested"]


# ---------------------------------------------------------------------------
# Validation: a pass must be backed, and a failure must never strand the run
# ---------------------------------------------------------------------------


def _to_patch_ready(rig: Rig) -> str:
    d = rig.driver
    session_id = d.create_session(d.create_project("preconditions"))
    d.step(session_id, "connect")
    d.step(session_id, "analysis")
    d.step(session_id, "workflows", {"workflow_ids": WORKFLOW_IDS})
    plan = d.step(session_id, "plan")["approval"]
    d.decide(plan["id"])
    d.step(session_id, "patch", {"approval_id": plan["id"]})
    return session_id


def _to_patch_approved(rig: Rig) -> tuple[str, str]:
    session_id = _to_patch_ready(rig)
    patch = rig.driver.step(session_id, "security-review")["approval"]
    rig.driver.decide(patch["id"])
    return session_id, str(patch["id"])


def _replace_stored_patch(rig: Rig, session_id: str) -> None:
    """The stored patch no longer matches what the approved plan generates."""
    session = asyncio.run(rig.store.get_session(session_id, OWNER))
    asyncio.run(
        rig.store.put_artifact(
            Artifact(
                session_id=session_id,
                project_id=session.project_id,
                kind=ArtifactKind.PATCH,
                payload={"base_commit": None, "files": []},
            )
        )
    )


def _repository_to_patch_approved(rig: Rig, name: str) -> tuple[str, str]:
    """A bound, elevated repository project waiting at the patch gate."""
    d = rig.driver
    project_id = d.create_project(name)
    bound = d.post(
        f"/api/projects/{project_id}/repository",
        {"repository_id": str(REPO_ID), "full_name": REPO, "base_branch": "main"},
    )
    assert bound.status_code == 200, bound.text
    session_id = d.create_session(project_id)
    d.step(session_id, "connect")
    d.step(session_id, "analysis")
    d.step(session_id, "workflows", {"workflow_ids": WORKFLOW_IDS})
    plan = d.step(session_id, "plan")["approval"]
    d.decide(plan["id"])
    d.step(session_id, "patch", {"approval_id": plan["id"]})
    patch = d.step(session_id, "security-review")["approval"]
    d.decide(patch["id"])
    assert d.post(f"/api/projects/{project_id}/access/elevate").status_code == 200
    return session_id, str(patch["id"])


def _assert_no_pull_request_can_follow(rig: Rig, session_id: str) -> None:
    d = rig.driver
    assert d.stored_state(session_id, OWNER) == "VALIDATION_FAILED"
    d.step(session_id, "pull-request/request", expect=409)
    assert not {target for _, target in d.transitions(session_id)} & PR_STATES
    assert not [c for c in rig.github.calls if c[0] != "GET" and c[1].startswith(f"/repos/{REPO}/")]


def test_the_pr_leg_validates_on_dependencies_from_the_install_step(rig: Rig) -> None:
    """The install ran networked and only there; validation ran without the
    network; and the report says where the dependency tree came from."""
    # The repository also commits a `node_modules`. It must be dropped, not
    # used — were it copied across, the install's copy would collide with it.
    rig.executor.ship_node_modules = True
    session_id = run_pr_leg(rig)
    payload = rig.driver.agent_read(session_id, "validation")["payload"]
    assert payload["validated"] is True
    dependencies = payload["dependencies"]
    assert dependencies["source"] == "install-step"
    install = dependencies["install"]
    assert install["status"] == "INSTALLED"
    assert (
        install["lockfile_sha256"] == hashlib.sha256(_lockfile_bytes(SCRIPTED_LOCKFILE)).hexdigest()
    )
    assert install["package_count"] == 2
    assert install["exit_code"] == 0
    assert install["argv"] == list(install_command("install").argv)

    installs = [networked for argv, networked in rig.executor.runs if argv[:2] == ("npm", "ci")]
    assert installs == [True], "the install must run once, in a networked workspace"
    checks = [
        networked
        for argv, networked in rig.executor.runs
        if argv[:2] in (("node", VITEST_CLI), ("npm", "run"))
    ]
    assert checks and not any(checks), "a validation command ran with the network"


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("no_lockfile", "no package-lock.json"),
        ("foreign_registry", "installs only from https://registry.npmjs.org/"),
        ("install_fails", "`npm ci` exited 1"),
    ],
)
def test_a_repository_the_install_step_cannot_serve_is_not_validated(
    rig: Rig, case: str, reason: str
) -> None:
    """No lockfile, a lockfile resolving elsewhere, or a failing install: each
    ends at VALIDATION_FAILED as "could not validate", with the install record
    stored and no pull request possible. The repository ships its own
    `node_modules` throughout — and it is never used."""
    rig.executor.ship_node_modules = True
    if case == "no_lockfile":
        rig.executor.lockfile = None
    elif case == "foreign_registry":
        foreign = json.loads(_lockfile_bytes(SCRIPTED_LOCKFILE))
        foreign["packages"]["node_modules/vitest"]["resolved"] = (
            "https://packages.example.test/vitest-3.2.0.tgz"
        )
        rig.executor.lockfile = foreign
    else:
        rig.executor.install_exit_code = 1

    session_id, approval_id = _repository_to_patch_approved(rig, case)
    validated = rig.driver.step(session_id, "validation", {"approval_id": approval_id})
    assert validated["state"] == "VALIDATION_FAILED"
    assert validated["detail"].startswith("Could not validate")
    assert reason in validated["detail"]

    payload = rig.driver.agent_read(session_id, "validation")["payload"]
    assert payload["validated"] is False
    install = payload["dependencies"]["install"]
    assert install["status"] == ("FAILED" if case == "install_fails" else "REFUSED")
    if case != "install_fails":
        assert not [a for a, _ in rig.executor.runs if a[:2] == ("npm", "ci")], "refused, yet ran"
    assert not [a for a, _ in rig.executor.runs if a[0] == "node"], "validated anyway"
    _assert_no_pull_request_can_follow(rig, session_id)


def test_a_repository_with_no_dependency_tree_cannot_pass_validation(rig: Rig) -> None:
    """The reviewer's round-1 finding: typecheck and build ran, every tool check
    was skipped, the score was 0, and the run reached VALIDATION_PASSED and then
    opened a pull request. A pass now requires the tool checks to have run —
    shown here with an install that succeeds but provides no test runner."""
    rig.executor.install_provides_vitest = False
    session_id, approval_id = _repository_to_patch_approved(rig, "no dependency tree")
    d = rig.driver

    validated = d.step(session_id, "validation", {"approval_id": approval_id})
    assert validated["state"] == "VALIDATION_FAILED"
    assert d.stored_state(session_id, OWNER) == "VALIDATION_FAILED"
    payload = d.agent_read(session_id, "validation")["payload"]
    assert payload["validated"] is False
    expected = {f"{kind}:{tool}" for kind in ("registration", "schema") for tool in WORKFLOW_IDS}
    assert expected <= set(payload["unexecuted_tool_checks"])
    # The executed checks did pass — the failure is the missing evidence.
    assert payload["report"]["checks"] and all(
        c["evidence"]["exit_code"] == 0 for c in payload["report"]["checks"]
    )
    last = d.events(session_id)[-1]
    assert last["detail"]["cause"].startswith("Could not validate")
    assert payload["dependencies"]["install"]["status"] == "INSTALLED"

    # And nothing downstream opens a pull request on it.
    _assert_no_pull_request_can_follow(rig, session_id)


@pytest.mark.parametrize(
    ("failure", "status", "state_after"),
    [
        ("no_executor", 503, "PATCH_APPROVAL_PENDING"),
        ("patch_changed", 409, "PATCH_APPROVAL_PENDING"),
        ("workspace_refused", 200, "VALIDATION_FAILED"),
    ],
)
def test_a_validation_precondition_failing_never_strands_the_run(
    rig: Rig, failure: str, status: int, state_after: str
) -> None:
    """VALIDATION_RUNNING has no retry self-loop, so a failure inside it is a
    run with no route out. Preconditions are checked before the first
    transition; anything failing after it becomes VALIDATION_FAILED."""
    d = rig.driver
    session_id, approval_id = _to_patch_approved(rig)
    app_state = d.client.app.state  # type: ignore[attr-defined]
    executor = app_state.executor
    if failure == "no_executor":
        app_state.executor = None
    elif failure == "patch_changed":
        _replace_stored_patch(rig, session_id)
    else:
        rig.executor.refuse_workspaces = True

    d.step(session_id, "validation", {"approval_id": approval_id}, expect=status)
    state = d.stored_state(session_id, OWNER)
    assert state != "VALIDATION_RUNNING"
    assert state == state_after

    if failure == "no_executor":
        # Nothing was consumed: attach a provider and the same decision works.
        app_state.executor = executor
        validated = d.step(session_id, "validation", {"approval_id": approval_id})
        assert validated["state"] == "VALIDATION_PASSED"
    if failure == "workspace_refused":
        payload = d.agent_read(session_id, "validation")["payload"]
        assert payload["completed"] is False
        assert "SandboxError" in payload["reason"]


def test_a_security_review_precondition_failing_never_strands_the_run(rig: Rig) -> None:
    """SECURITY_REVIEW_RUNNING has no retry self-loop either."""
    session_id = _to_patch_ready(rig)
    _replace_stored_patch(rig, session_id)
    rig.driver.step(session_id, "security-review", expect=409)
    assert rig.driver.stored_state(session_id, OWNER) == "PATCH_READY"


# ---------------------------------------------------------------------------
# Every edge in the table
# ---------------------------------------------------------------------------


def _rejections(rig: Rig) -> str:
    """PR → patch → plan → selection: each rejection returns one step, and a
    gate reached again refuses the approval given before the rejection."""
    d = rig.driver
    project_id = d.create_project("rejections")
    bound = d.post(
        f"/api/projects/{project_id}/repository",
        {"repository_id": str(REPO_ID), "full_name": REPO, "base_branch": "main"},
    )
    assert bound.status_code == 200, bound.text
    session_id = d.create_session(project_id)
    assert d.step(session_id, "connect")["state"] == "ANALYSIS_PENDING"
    d.step(session_id, "analysis")
    d.step(session_id, "workflows", {"workflow_ids": WORKFLOW_IDS})
    plan = d.step(session_id, "plan")["approval"]
    d.decide(plan["id"])
    d.step(session_id, "patch", {"approval_id": plan["id"]})
    patch = d.step(session_id, "security-review")["approval"]
    d.decide(patch["id"])
    d.step(session_id, "validation", {"approval_id": patch["id"]})
    assert d.post(f"/api/projects/{project_id}/access/elevate").status_code == 200
    pr = d.step(session_id, "pull-request/request")["approval"]

    d.decide(pr["id"], "REJECTED")
    back = d.step(session_id, "reject", {"approval_id": pr["id"]})
    assert back["state"] == "PATCH_APPROVAL_PENDING"
    # The earlier PATCH approval still covers the unchanged patch, and is refused.
    d.step(session_id, "validation", {"approval_id": patch["id"]}, expect=403)

    d.decide(back["approval"]["id"], "REJECTED")
    back = d.step(session_id, "reject", {"approval_id": back["approval"]["id"]})
    assert back["state"] == "TOOL_PLAN_APPROVAL_PENDING"
    d.step(session_id, "patch", {"approval_id": plan["id"]}, expect=403)

    d.decide(back["approval"]["id"], "REJECTED")
    back = d.step(session_id, "reject", {"approval_id": back["approval"]["id"]})
    assert back["state"] == "WORKFLOW_SELECTION_PENDING"
    assert back["approval"] is None
    return session_id


def _retries_and_failure_loops(rig: Rig) -> str:
    d = rig.driver
    session_id = d.create_session(d.create_project("retries"))
    d.step(session_id, "connect")

    rig.gemini.queue("CodebaseAnalysis", GeminiTransportError("scripted outage", retryable=False))
    d.step(session_id, "analysis", expect=409)
    assert d.stored_state(session_id, OWNER) == "ANALYSIS_RUNNING"
    d.step(session_id, "analysis")

    d.step(session_id, "workflows", {"workflow_ids": WORKFLOW_IDS})
    rig.gemini.queue("ProposedToolPlan", BAD_PLAN, BAD_PLAN, BAD_PLAN)
    d.step(session_id, "plan", expect=409)
    assert d.stored_state(session_id, OWNER) == "TOOL_PLAN_RUNNING"
    plan = d.step(session_id, "plan")["approval"]
    d.decide(plan["id"])
    d.step(session_id, "patch", {"approval_id": plan["id"]})

    rig.gemini.queue("SecurityReport", REVIEW_BLOCK)
    assert d.step(session_id, "security-review")["state"] == "SECURITY_REVIEW_FAILED"
    assert d.step(session_id, "patch")["state"] == "PATCH_READY"
    patch = d.step(session_id, "security-review")["approval"]
    d.decide(patch["id"])

    rig.executor.fail_vitest = True
    assert d.step(session_id, "validation", {"approval_id": patch["id"]})["state"] == (
        "VALIDATION_FAILED"
    )
    rig.executor.fail_vitest = False
    assert d.step(session_id, "patch")["state"] == "PATCH_READY"
    patch = d.step(session_id, "security-review")["approval"]
    d.decide(patch["id"])
    assert d.step(session_id, "validation", {"approval_id": patch["id"]})["state"] == (
        "VALIDATION_PASSED"
    )
    return session_id


def _generation_retry(rig: Rig) -> str:
    """A credential-shaped description survives planning and is refused by the
    generator's outbound scan; retrying re-enters generation and is refused
    again, because generation is deterministic."""
    d = rig.driver
    session_id = d.create_session(d.create_project("generation retry"))
    d.step(session_id, "connect")
    d.step(session_id, "analysis")
    d.step(session_id, "workflows", {"workflow_ids": WORKFLOW_IDS})
    rig.gemini.queue("ProposedToolPlan", SECRET_PLAN)
    plan = d.step(session_id, "plan")["approval"]
    d.decide(plan["id"])
    refused = d.step(session_id, "patch", {"approval_id": plan["id"]}, expect=409)
    assert "credential-shaped" in refused["detail"]
    assert PLANTED_KEY not in refused["detail"]
    d.step(session_id, "patch", expect=409)
    assert d.stored_state(session_id, OWNER) == "GENERATION_RUNNING"
    return session_id


def _pr_with_a_failed_first_attempt(rig: Rig) -> str:
    rig.github.fail_next_pull = True
    d = rig.driver
    project_id = d.create_project("pr retry")
    session_id = d.create_session(project_id)
    asked = d.agent(session_id, "repository", {"repository_full_name": REPO, "branch": "main"})
    d.decide(asked["approval_id"])
    d.step(session_id, "connect", {"repository_binding_approval_id": asked["approval_id"]})
    d.step(session_id, "analysis")
    d.step(session_id, "workflows", {"workflow_ids": WORKFLOW_IDS})
    plan = d.step(session_id, "plan")["approval"]
    d.decide(plan["id"])
    d.step(session_id, "patch", {"approval_id": plan["id"]})
    patch = d.step(session_id, "security-review")["approval"]
    d.decide(patch["id"])
    d.step(session_id, "validation", {"approval_id": patch["id"]})
    d.post(f"/api/projects/{project_id}/access/elevate")
    pr = d.step(session_id, "pull-request/request")["approval"]
    d.decide(pr["id"])
    failed = d.step(session_id, "pull-request", {"approval_id": pr["id"]}, expect=409)
    assert "was removed" in failed["detail"]
    assert d.stored_state(session_id, OWNER) == "PR_CREATING"
    assert d.step(session_id, "pull-request")["state"] == "COMPLETE"
    deleted = [p for m, p, _ in rig.github.calls if m == "DELETE"]
    assert deleted and all("/heads/mcpforge/" in p for p in deleted)
    return session_id


def test_every_transition_in_the_table_is_driven(rig: Rig) -> None:
    """The acceptance criterion's "every state transition asserted", made a
    check that fails: the union of edges the machine recorded across these runs
    must equal the table, so a transition no run drives is reported by name."""
    sessions = [
        run_analysis_leg(rig),
        run_pr_leg(rig),
        _rejections(rig),
        _retries_and_failure_loops(rig),
        _generation_retry(rig),
        _pr_with_a_failed_first_attempt(rig),
    ]
    driven = {edge for session_id in sessions for edge in rig.driver.transitions(session_id)}
    assert driven <= ALL_TRANSITIONS, f"edges outside the table: {sorted(driven - ALL_TRANSITIONS)}"
    missing = sorted(ALL_TRANSITIONS - driven)
    assert not missing, f"transitions no run drove: {missing}"
