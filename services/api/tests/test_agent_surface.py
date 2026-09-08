"""The agent surface — F7-02, F7-03, F7-04.

These are the tests for the product's central claim: an agent can drive
MCPForge, and still cannot approve anything. They are written to fail if that
stops being true, not to demonstrate that the endpoints exist.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mcpforge.auth.identity import AuthError, VerifiedIdentity
from mcpforge.config import Settings
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.main import create_app
from mcpforge.models.core import Artifact, ArtifactKind, artifact_hash
from mcpforge.models.patch import ChangeKind, FileChange, GeneratedPatch
from mcpforge.store.memory import InMemoryStore
from tests.structure import SRC, python_files

OWNER = "uid-owner"
OTHER = "uid-stranger"


class TokenIsUid:
    async def verify(self, raw_token: str) -> VerifiedIdentity:
        if not raw_token.startswith("uid-"):
            raise AuthError("bad token")
        return VerifiedIdentity(subject=raw_token, issuer="test")


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(
        settings,
        token_verifier=TokenIsUid(),
        store=InMemoryStore(),
        gemini=FakeGeminiProvider([]),
    )
    with TestClient(app) as c:
        yield c


def auth(uid: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def make_session(client: TestClient, uid: str = OWNER) -> str:
    project = client.post("/api/projects", json={"name": "hotel"}, headers=auth(uid)).json()
    return str(
        client.post(f"/api/projects/{project['id']}/sessions", headers=auth(uid)).json()["id"]
    )


def seed_plan(client: TestClient, session_id: str, uid: str = OWNER) -> None:
    """Store a TOOL_PLAN artifact.

    The plan is produced server-side by the architect agent, not by an agent
    tool, so it is written straight to the store here rather than faked through
    an endpoint that does not exist.
    """
    store = client.app.state.store  # type: ignore[attr-defined]
    session = asyncio.run(store.get_session(session_id, uid))
    asyncio.run(
        store.put_artifact(
            Artifact(
                session_id=session_id,
                project_id=session.project_id,
                kind=ArtifactKind.TOOL_PLAN,
                payload={"tools": ["searchRooms"]},
            )
        )
    )


#: Every gate-opening endpoint, with a body that passes validation.
#:
#: These lists are hand-maintained and are **not** claimed to be exhaustive. An
#: earlier version of this file enumerated the router and failed when a route
#: appeared in neither list; that enumerator was removed after three review
#: rounds found a different blind spot in each version of it (a nested include,
#: a second router, a mounted sub-app). Exhaustiveness over routes is not how
#: this file establishes its security property any more — that job belongs to
#: the route-independent property tests below, which scan every backend module
#: and hold no matter how a route is mounted. These lists exist so the
#: behavioural sweeps have concrete endpoints to drive.
MUTATION_TOOLS: list[tuple[str, dict[str, object]]] = [
    ("repository", {"repository_full_name": "acme/site", "branch": "main"}),
    ("workflows", {"workflow_ids": ["searchRooms"]}),
    ("plan/approve", {}),
    ("pull-request", {"title": "Add WebMCP tools"}),
]

#: Endpoints that record a request but open no gate, because the stage they ask
#: for is not wired to the orchestrator yet.
STAGE_TOOLS: list[tuple[str, dict[str, object]]] = [
    ("analysis", {}),
    ("patch", {"summary": "generate"}),
    ("security-review", {}),
    ("validation", {}),
]


def seed_prerequisites(client: TestClient, session_id: str) -> None:
    """Whatever each gate-opening endpoint needs in order to reach its gate."""
    seed_plan(client, session_id)
    seed_patch(client, session_id)


#: Every GET on the agent router, and what a read must never do.
#:
#: A GET is not automatically safe. The reviewer demonstrated a GET endpoint on
#: this router that self-approved any gate while all 767 tests stayed green: it
#: was not a POST, so the vocabulary test passed it; its path held neither
#: "decide" nor "approvals"; it constructed no `Approval`; it passed no `origin`.
#: Every structural guard was satisfied by an endpoint doing the exact inverse of
#: this phase's claim. Reads are therefore enumerated and constrained too.
READ_TOOLS: list[str] = ["status", "workflows", "plan", "validation"]


#: The three fields that, together, are a recorded human decision.
DECISION_FIELDS = {"status", "actor_uid", "decided_at"}

#: The one module allowed to write or persist one.
DECISION_HOME = "mcpforge/api/approvals.py"

#: `agents/interaction.py::commit_decision` returns a decided `Approval` as a
#: *value*. It is allowed to build one and cannot record one: persisting it
#: needs `Store.update_approval`, and sub-check (c) below permits that call in
#: `DECISION_HOME` alone. The guarantee is the conjunction of the two — a value
#: nothing can store is not a decision.
COPY_ALLOWED = {DECISION_HOME, "mcpforge/agents/interaction.py"}


def _root_name(node: ast.expr) -> str | None:
    """The base identifier of `x`, `x.y` or `x.y.z`, if there is one."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def test_only_one_module_may_write_an_approval_decision() -> None:
    """The security property, stated once and route-independently.

    Seven review rounds were spent trying to prove "no route can approve
    anything" by enumerating routes. Each enumerator had a different blind spot
    — a nested include, a WebSocket, a second router, a mounted sub-app — and
    each fix traded away what the previous one caught. Enumeration is the wrong
    shape for this: there is no bounded list of ways to add a route.

    So the property is about *writing a decision*, not about a route. Three
    ways exist to move an approval off PENDING, and all three are swept across
    every backend module:

    (a) assigning a decision field — `approval.status = APPROVED`
    (b) copying one on — `pending.model_copy(update={"status": APPROVED})`
    (c) persisting one — `store.update_approval(...)`, `create_approval(...)`,
        or re-entering the project's own `decide_approval(...)`

    (b) and (c) exist because round 7 proved (a) alone was not enough: a second
    `APIRouter(prefix="/api/agent")` in a new module flipped approvals to
    APPROVED through `model_copy` + `update_approval`, and the whole suite
    stayed green while the gate opened with no human decision. `decide_approval`
    joined (c) in round 8 for the same reason: a second agent router that simply
    *called* the real decision handler reached APPROVED with no human, and
    stamped the timeline `HUMAN:approval.decided` while doing it.

    None of this looks at routes, so a new endpoint cannot evade it regardless
    of how it is mounted, what verb it uses, or whether it reaches the OpenAPI
    schema.

    **What this test does and does not prove.** Sub-checks (b) and (c) match on
    *names* in the AST, so they catch straightforwardly-written paths — the ones
    a mistake, a refactor, or a generated route would actually take. They do not
    and cannot decide the general case: `getattr(store, "update_approval")` with
    a non-literal `update=` dict built under a non-approval-named local defeats
    any name-based check. No AST rule closes that, and widening the denylist
    only moves the boundary. The guarantee is the *conjunction* of this sweep
    with the behavioural gate tests below — `test_every_mutation_tool_stops_at_a_
    human_decision`, `test_a_mutation_tool_leaves_the_gate_shut`,
    `test_a_read_tool_changes_nothing` and
    `test_no_agent_endpoint_transitions_the_session` — which assert against the
    *stored record* and hold however the write was spelled.
    """
    assigned: list[str] = []
    copied: list[str] = []
    persisted: list[str] = []

    for path in python_files():
        rel = str(path.relative_to(SRC))
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # (a) `approval.status = ...`
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in DECISION_FIELDS
                    and isinstance(target.value, ast.Name)
                    and "approval" in target.value.id.lower()
                ):
                    assigned.append(f"{rel}:{target.lineno}")

            if not isinstance(node, ast.Call):
                continue

            # (b) `x.model_copy(update={"status": ...})`
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"model_copy", "copy"}:
                receiver = _root_name(node.func.value) or ""
                for kw in node.keywords:
                    if kw.arg != "update":
                        continue
                    if not isinstance(kw.value, ast.Dict):
                        # An `update=` we cannot read is only a hole when it is
                        # applied to something approval-shaped; flag that case
                        # rather than let a dict built elsewhere slip past.
                        if "approval" in receiver.lower():
                            copied.append(f"{rel}:{node.lineno}")
                        continue
                    keys = {
                        k.value
                        for k in kw.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
                    if keys & DECISION_FIELDS:
                        copied.append(f"{rel}:{node.lineno}")

            # (c) persisting an approval, however it was built.
            called = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if isinstance(node.func, ast.Name):
                called = node.func.id
            if called in {"update_approval", "create_approval", "decide_approval"}:
                persisted.append(f"{rel}:{node.lineno}")

    # Self-guards. Each sub-check must have found its known-good instance, or
    # it is scanning nothing and the assertions below are vacuous.
    assert assigned, "sub-check (a) found no decision-field assignment anywhere"
    assert copied, "sub-check (b) found no decision-carrying model_copy anywhere"
    assert persisted, "sub-check (c) found no approval-persisting call anywhere"

    def outside(found: list[str], allowed: set[str]) -> list[str]:
        return sorted(f for f in found if f.rsplit(":", 1)[0] not in allowed)

    violations = {
        "assigns a decision field": outside(assigned, {DECISION_HOME}),
        "copies a decision onto an approval": outside(copied, COPY_ALLOWED),
        "persists an approval": outside(persisted, {DECISION_HOME}),
    }
    reported = [f"{what}: {', '.join(where)}" for what, where in violations.items() if where]
    assert not reported, (
        "a decision is written outside api/approvals.py:\n"
        + "\n".join(reported)
        + "\nOnly decide_approval may record a human decision."
    )


def test_the_agent_module_never_writes_an_approval_decision() -> None:
    """The same property, aimed at the module an agent can actually reach.

    Narrower and blunter than the sweep above, and it holds no matter what
    routes `api/agent.py` grows: if the module cannot name a decision field or
    an approval-updating call, nothing it serves can decide anything.
    """
    source = (SRC / "mcpforge" / "api" / "agent.py").read_text()
    tree = ast.parse(source)

    banned_calls = {"update_approval", "decide_approval"}
    called = [
        f"agent.py:{node.lineno}: {node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in banned_calls
    ]
    assert not called, "the agent module can decide an approval: " + ", ".join(called)

    assigned = [
        f"agent.py:{target.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and target.attr in {"status", "actor_uid", "decided_at"}
    ]
    assert not assigned, "the agent module writes a decision field: " + ", ".join(assigned)


@pytest.mark.parametrize("path", READ_TOOLS)
def test_a_read_tool_changes_nothing(client: TestClient, path: str) -> None:
    """A GET must not write an event, and must not touch an approval.

    This is what makes classifying a route as a read mean something. The
    reviewer's self-approving GET is caught here even if it were listed in
    READ_TOOLS.
    """
    session_id = make_session(client)
    seed_prerequisites(client, session_id)
    asked = client.post(
        f"/api/agent/sessions/{session_id}/workflows",
        json={"workflow_ids": ["a"]},
        headers=auth(),
    ).json()

    before = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()
    state_before = _session_state(client, session_id)
    response = client.get(f"/api/agent/sessions/{session_id}/{path}", headers=auth())
    assert response.status_code == 200, path
    after = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()

    assert len(after) == len(before), f"GET {path} wrote {len(after) - len(before)} event(s)"
    assert _session_state(client, session_id) == state_before, f"GET {path} moved the session"
    approval = client.get(f"/api/approvals/{asked['approval_id']}", headers=auth()).json()
    assert approval["status"] == "PENDING", f"GET {path} decided an approval"
    assert approval["actor_uid"] is None


def _session_state(client: TestClient, session_id: str, uid: str = OWNER) -> str:
    """The session's state, read out of the store rather than a response body.

    A response could report the old state while the record moved on.
    """
    store = client.app.state.store  # type: ignore[attr-defined]
    return str(asyncio.run(store.get_session(session_id, uid)).state.value)


@pytest.mark.parametrize(("path", "payload"), MUTATION_TOOLS + STAGE_TOOLS)
def test_no_agent_endpoint_transitions_the_session(
    client: TestClient, path: str, payload: dict[str, object]
) -> None:
    """`api/agent.py` and `02_ARCHITECTURE.md` §10.1 both claim no agent endpoint
    transitions the session, and both said "each is tested" when this one was
    not: inserting a `session.state = ...; await store.update_session(session)`
    into `select_workflows` left the whole suite green.

    An agent moving the run forward is the gate opening without a decision —
    the state is what the orchestrator reads. So the claim is checked
    behaviourally, against the stored record, for every agent POST.
    """
    session_id = make_session(client)
    seed_prerequisites(client, session_id)

    before = _session_state(client, session_id)
    response = client.post(f"/api/agent/sessions/{session_id}/{path}", json=payload, headers=auth())
    assert response.status_code == 200, f"{path}: {response.text}"

    assert _session_state(client, session_id) == before, (
        f"POST {path} moved the session from {before} to "
        f"{_session_state(client, session_id)} with no human decision"
    )


# -- F7-02 read tools -------------------------------------------------------


def test_an_unauthenticated_agent_gets_an_error_not_data(client: TestClient) -> None:
    session_id = make_session(client)
    response = client.get(f"/api/agent/sessions/{session_id}/status")
    assert response.status_code == 401
    assert "name" not in response.text


@pytest.mark.parametrize("path", READ_TOOLS)
def test_an_agent_cannot_read_another_users_session(client: TestClient, path: str) -> None:
    """Driven by READ_TOOLS, so a new read route is covered the moment it is
    classified — it was a hardcoded four-tuple that drifted from the router."""
    session_id = make_session(client, OWNER)
    response = client.get(f"/api/agent/sessions/{session_id}/{path}", headers=auth(OTHER))
    assert response.status_code == 404


@pytest.mark.parametrize(("path", "payload"), MUTATION_TOOLS + STAGE_TOOLS)
def test_another_user_cannot_post_to_any_agent_endpoint(
    client: TestClient, path: str, payload: dict[str, object]
) -> None:
    """The reviewer's mutation showed only the READ path was covered: dropping
    owner scoping left every mutation endpoint untested."""
    session_id = make_session(client, OWNER)
    seed_prerequisites(client, session_id)
    response = client.post(
        f"/api/agent/sessions/{session_id}/{path}", json=payload, headers=auth(OTHER)
    )
    assert response.status_code == 404, path


@pytest.mark.parametrize(("path", "payload"), STAGE_TOOLS)
def test_a_stage_that_is_not_wired_does_not_claim_to_have_started(
    client: TestClient, path: str, payload: dict[str, object]
) -> None:
    """CLAUDE.md §9 — no hardcoded value that makes a check look passed."""
    session_id = make_session(client)
    seed_prerequisites(client, session_id)
    body = client.post(
        f"/api/agent/sessions/{session_id}/{path}", json=payload, headers=auth()
    ).json()
    assert body["started"] is False
    assert "not yet connected" in body["detail"]


@pytest.mark.parametrize(("path", "payload"), STAGE_TOOLS)
def test_a_stage_that_is_not_wired_opens_no_gate(
    client: TestClient, path: str, payload: dict[str, object]
) -> None:
    """An approval nothing can act on is worse than no approval: it reads as
    granted to the developer and authorises nothing."""
    session_id = make_session(client)
    seed_prerequisites(client, session_id)
    before = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()
    opened_before = len([e for e in before if e["kind"] == "approval.requested"])

    client.post(f"/api/agent/sessions/{session_id}/{path}", json=payload, headers=auth())

    after = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()
    opened_after = len([e for e in after if e["kind"] == "approval.requested"])
    assert opened_after == opened_before, f"{path} opened a gate it cannot fulfil"


def test_project_status_reports_the_real_state(client: TestClient) -> None:
    session_id = make_session(client)
    body = client.get(f"/api/agent/sessions/{session_id}/status", headers=auth()).json()
    assert body["name"] == "hotel"
    assert body["access_mode"] == "READ_ONLY"
    assert body["is_demo"] is True
    assert body["repository_full_name"] is None


def test_a_missing_artifact_reads_as_unavailable_rather_than_an_error(client: TestClient) -> None:
    session_id = make_session(client)
    body = client.get(f"/api/agent/sessions/{session_id}/plan", headers=auth()).json()
    assert body["available"] is False
    assert body["artifact_hash"] is None
    assert body["payload"] == {}


# -- F7-03 the gate ---------------------------------------------------------


@pytest.mark.parametrize(("path", "payload"), MUTATION_TOOLS)
def test_every_mutation_tool_stops_at_a_human_decision(
    client: TestClient, path: str, payload: dict[str, object]
) -> None:
    session_id = make_session(client)
    seed_prerequisites(client, session_id)
    body = client.post(
        f"/api/agent/sessions/{session_id}/{path}", json=payload, headers=auth()
    ).json()

    assert body["status"] == "awaiting_human_approval"
    approval = client.get(f"/api/approvals/{body['approval_id']}", headers=auth()).json()
    assert approval["status"] == "PENDING"
    assert approval["actor_uid"] is None

    # Read it back out of the store too. Asserting only on the response would
    # pass even if the approval were persisted as already-approved.
    store = client.app.state.store  # type: ignore[attr-defined]
    stored = asyncio.run(store.get_approval(body["approval_id"], OWNER))
    assert stored.status.value == "PENDING"
    assert stored.decided_at is None


@pytest.mark.parametrize(("path", "payload"), MUTATION_TOOLS)
def test_a_mutation_tool_leaves_the_gate_shut(
    client: TestClient, path: str, payload: dict[str, object]
) -> None:
    """The gate check is what the orchestrator consults. Calling the tool must
    not open it."""
    session_id = make_session(client)
    seed_prerequisites(client, session_id)
    body = client.post(
        f"/api/agent/sessions/{session_id}/{path}", json=payload, headers=auth()
    ).json()

    gate = client.get(
        f"/api/sessions/{session_id}/gate",
        params={"gate": body["gate"], "artifact_hash": body["artifact_hash"]},
        headers=auth(),
    ).json()
    assert gate["open"] is False


def test_the_agent_surface_exposes_no_way_to_decide_an_approval(client: TestClient) -> None:
    """There must be no agent-reachable decide path — not a differently named
    one, and not a second implementation."""
    paths = list(client.app.openapi()["paths"])  # type: ignore[attr-defined]
    agent_paths = [p for p in paths if p.startswith("/api/agent")]
    assert agent_paths, "the agent router is not mounted"
    for path in agent_paths:
        assert "decide" not in path
        assert "approvals" not in path


def test_an_agent_cannot_decide_by_claiming_approval_in_the_body(client: TestClient) -> None:
    """Model output is not authorization. Extra fields must not be honoured."""
    session_id = make_session(client)
    body = client.post(
        f"/api/agent/sessions/{session_id}/workflows",
        json={
            "workflow_ids": ["searchRooms"],
            "status": "APPROVED",
            "approved": True,
            "actor_uid": OWNER,
        },
        headers=auth(),
    ).json()

    approval = client.get(f"/api/approvals/{body['approval_id']}", headers=auth()).json()
    assert approval["status"] == "PENDING"
    assert approval["actor_uid"] is None


def test_approve_webmcp_plan_does_not_approve_the_plan(client: TestClient) -> None:
    """The tool is named for the agent's intent. Its effect is a request."""
    session_id = make_session(client)
    seed_plan(client, session_id)
    response = client.post(f"/api/agent/sessions/{session_id}/plan/approve", headers=auth())
    assert response.status_code == 200
    body = response.json()
    approval = client.get(f"/api/approvals/{body['approval_id']}", headers=auth()).json()
    assert approval["status"] == "PENDING"


def test_the_full_loop_agent_asks_human_decides_gate_opens(client: TestClient) -> None:
    """The acceptance criterion for F7-03, end to end."""
    session_id = make_session(client)
    asked = client.post(
        f"/api/agent/sessions/{session_id}/repository",
        json={"repository_full_name": "acme/site", "branch": "main"},
        headers=auth(),
    ).json()

    def gate_open() -> bool:
        return bool(
            client.get(
                f"/api/sessions/{session_id}/gate",
                params={"gate": asked["gate"], "artifact_hash": asked["artifact_hash"]},
                headers=auth(),
            ).json()["open"]
        )

    assert gate_open() is False

    decided = client.post(
        f"/api/approvals/{asked['approval_id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    ).json()
    assert decided["actor_uid"] == OWNER

    assert gate_open() is True


def test_another_user_cannot_decide_an_approval_the_agent_opened(client: TestClient) -> None:
    session_id = make_session(client, OWNER)
    asked = client.post(
        f"/api/agent/sessions/{session_id}/workflows",
        json={"workflow_ids": ["a"]},
        headers=auth(OWNER),
    ).json()
    response = client.post(
        f"/api/approvals/{asked['approval_id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(OTHER),
    )
    assert response.status_code == 404


def test_a_pull_request_needs_a_patch_first(client: TestClient) -> None:
    session_id = make_session(client)
    response = client.post(
        f"/api/agent/sessions/{session_id}/pull-request",
        json={"title": "Add WebMCP tools"},
        headers=auth(),
    )
    assert response.status_code == 409


def seed_patch(client: TestClient, session_id: str, uid: str = OWNER) -> str:
    """Store a PATCH artifact in the shape `github/writer.py` hashes.

    The payload must be exactly what `GeneratedPatch.hashable()` returns, since
    that is the value the writer requires both approvals to cover.
    """
    store = client.app.state.store  # type: ignore[attr-defined]
    session = asyncio.run(store.get_session(session_id, uid))
    patch = GeneratedPatch(
        base_commit="c" * 40,
        summary="Register the searchRooms tool",
        files=[
            FileChange(
                path="src/webmcp/tools.ts",
                kind=ChangeKind.ADD,
                contents="export {};\n",
                rationale="Registers the generated tool.",
            )
        ],
    )
    artifact = asyncio.run(
        store.put_artifact(
            Artifact(
                session_id=session_id,
                project_id=session.project_id,
                kind=ArtifactKind.PATCH,
                payload=patch.hashable(),
            )
        )
    )
    assert artifact.hash == artifact_hash(patch.hashable())
    return str(artifact.hash)


def test_the_pr_approval_binds_to_the_hash_the_writer_requires(client: TestClient) -> None:
    """`github/writer.py` refuses unless both approvals cover
    `artifact_hash(patch.hashable())`. An approval bound to anything else reads
    as granted to the developer and authorises nothing."""
    session_id = make_session(client)
    expected = seed_patch(client, session_id)

    asked = client.post(
        f"/api/agent/sessions/{session_id}/pull-request",
        json={"title": "Add WebMCP tools"},
        headers=auth(),
    ).json()

    assert asked["artifact_hash"] == expected

    client.post(
        f"/api/approvals/{asked['approval_id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    )
    store = client.app.state.store  # type: ignore[attr-defined]
    approval = asyncio.run(store.get_approval(asked["approval_id"], OWNER))
    assert approval.covers(expected), "the writer would refuse this approval"


def test_requesting_a_pr_does_not_rewrite_the_patch_artifact(client: TestClient) -> None:
    """Rewriting it would change the patch's derived hash, so the approval the
    human just gave would stop covering the patch that gets written."""
    session_id = make_session(client)
    before = seed_patch(client, session_id)

    client.post(
        f"/api/agent/sessions/{session_id}/pull-request",
        json={"title": "Add WebMCP tools"},
        headers=auth(),
    )

    store = client.app.state.store  # type: ignore[attr-defined]
    after = asyncio.run(store.get_artifact(session_id, ArtifactKind.PATCH, OWNER))
    assert after is not None
    assert after.hash == before, "the PR request overwrote the approved patch"


def test_regenerating_an_artifact_invalidates_its_approval(client: TestClient) -> None:
    """Approval binds to content. Different content, different decision."""
    session_id = make_session(client)
    first = client.post(
        f"/api/agent/sessions/{session_id}/workflows",
        json={"workflow_ids": ["a"]},
        headers=auth(),
    ).json()
    client.post(
        f"/api/approvals/{first['approval_id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    )
    second = client.post(
        f"/api/agent/sessions/{session_id}/workflows",
        json={"workflow_ids": ["a", "b"]},
        headers=auth(),
    ).json()

    assert second["artifact_hash"] != first["artifact_hash"]
    gate = client.get(
        f"/api/sessions/{session_id}/gate",
        params={"gate": "WORKFLOW_SELECTION", "artifact_hash": second["artifact_hash"]},
        headers=auth(),
    ).json()
    assert gate["open"] is False


# -- F7-04 origin -----------------------------------------------------------


def test_agent_actions_are_recorded_as_agent_origin(client: TestClient) -> None:
    session_id = make_session(client)
    client.post(f"/api/agent/sessions/{session_id}/analysis", headers=auth())
    events = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()
    requested = [e for e in events if e["kind"] == "analysis.requested"]
    assert requested and all(e["origin"] == "AGENT" for e in requested)


def test_a_client_supplied_origin_is_ignored(client: TestClient) -> None:
    """The threat is an agent disguising its actions as a human's."""
    session_id = make_session(client)
    client.post(
        f"/api/agent/sessions/{session_id}/workflows",
        json={"workflow_ids": ["a"], "origin": "HUMAN"},
        headers=auth(),
    )
    events = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()
    agent_events = [e for e in events if e["kind"] == "approval.requested"]
    assert agent_events
    assert all(e["origin"] == "AGENT" for e in agent_events)


def test_a_human_decision_is_recorded_as_human_even_on_an_agent_opened_approval(
    client: TestClient,
) -> None:
    session_id = make_session(client)
    asked = client.post(
        f"/api/agent/sessions/{session_id}/workflows",
        json={"workflow_ids": ["a"]},
        headers=auth(),
    ).json()
    client.post(
        f"/api/approvals/{asked['approval_id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    )
    events = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()
    decided = [e for e in events if e["kind"] == "approval.decided"]
    assert decided and all(e["origin"] == "HUMAN" for e in decided)


def test_no_agent_endpoint_can_write_a_human_origin_event(client: TestClient) -> None:
    """Sweep: after exercising every agent tool, nothing it wrote is HUMAN."""
    session_id = make_session(client)
    client.post(f"/api/agent/sessions/{session_id}/analysis", headers=auth())
    client.post(f"/api/agent/sessions/{session_id}/security-review", headers=auth())
    client.post(f"/api/agent/sessions/{session_id}/validation", headers=auth())
    for path, payload in MUTATION_TOOLS:
        client.post(f"/api/agent/sessions/{session_id}/{path}", json=payload, headers=auth())

    events = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()
    assert events
    assert not [e for e in events if e["origin"] == "HUMAN"]


# -- F7-04 structural: origin cannot come from the caller -------------------


def _api_modules() -> list[pathlib.Path]:
    api = SRC / "mcpforge" / "api"
    return sorted(p for p in api.glob("*.py") if p.name != "__init__.py")


def _annotation_names(tree: ast.Module) -> set[str]:
    """Every identifier used as a function-parameter annotation in a module.

    This is how a Pydantic model becomes a *request body*: FastAPI binds a body
    to a parameter annotated with the model. Response models appear as return
    annotations and in `response_model=`, never here.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is None or arg.annotation is None:
                continue
            for sub in ast.walk(arg.annotation):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
    return names


def test_no_request_model_declares_an_origin_field() -> None:
    """If a request body could carry an origin, FastAPI would bind it and the
    server-side derivation would be bypassed. Parsed, not grepped: comments and
    docstrings explaining this rule must not satisfy it.

    Any `BaseModel` bound to a parameter counts, not only one whose name ends
    `Body` — that naming convention was the whole check until round 7, so a
    model called anything else was free to carry an origin. Response models are
    excluded because FastAPI never binds a caller's input to one; they legitimately
    carry the origin the server derived, on the way out.
    """
    declaring_origin: list[str] = []
    bound_models: list[str] = []
    offenders: list[str] = []

    for path in _api_modules():
        tree = ast.parse(path.read_text())
        bound = _annotation_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            if "BaseModel" not in bases:
                continue
            if node.name in bound:
                bound_models.append(f"{path.name}:{node.name}")
            has_origin = any(
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id == "origin"
                for stmt in node.body
            )
            if not has_origin:
                continue
            declaring_origin.append(f"{path.name}:{node.name}")
            if node.name in bound:
                offenders.append(f"{path.name}:{node.name}.origin")

    assert declaring_origin, "no BaseModel in the API layer declares origin — scanning nothing"
    assert bound_models, "no BaseModel is bound to a parameter — the body detection is broken"
    assert not offenders, "request body carries a caller-settable origin:\n" + "\n".join(offenders)


def _module_level_names(tree: ast.Module) -> set[str]:
    """Names bound at module scope. Nothing at module scope can see a request."""
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return names


def _bound_names_in(func: ast.AST) -> set[str]:
    """Every name rebound anywhere inside `func` — parameters plus assignments.

    A module-level constant is only server-derived while the function leaves it
    alone. `ORIGIN = Origin(payload["origin"])` inside a handler rebinds the
    name locally, and a check that looked only at parameters would still read it
    as the module constant.
    """
    names: set[str] = set()

    def add(target: ast.expr) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                add(element)
        elif isinstance(target, ast.Starred):
            add(target.value)

    if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
        args = func.args
        names.update(
            a.arg
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            if a is not None
        )

    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                add(target)
        elif isinstance(
            node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr | ast.For | ast.AsyncFor
        ):
            add(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            add(node.optional_vars)
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            names.add(node.name)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)

    return names


def test_no_api_module_takes_an_events_origin_from_its_caller() -> None:
    """Origin is derived server-side in every API module, not just `agent.py`.

    Stated as an **allowlist**, not a denylist. Until round 8 the rule was "an
    `origin=` may not be *rooted at a parameter*", which is a guess about the
    shape of the attack. It missed the shape that matters:

        payload = await request.json()
        RunEvent(..., origin=Origin(payload["origin"]))

    — a subscript, so it has no root name at all, so nothing flagged it, and an
    agent could label its own event HUMAN. Widening the denylist would invite
    the next variant. So the check now enumerates what is *legitimate* and
    rejects everything else. An `origin=` argument in `api/` must be one of:

    (1) an `Origin.<member>` — the server naming the origin outright;
    (2) a module-level constant, e.g. `agent.py`'s `ORIGIN`, *that the function
        does not rebind* — module scope cannot see a request, so an untouched
        module name is server-derived by construction. Round 9 found the round-8
        version of this clause excluded only parameters, so
        `ORIGIN = Origin(payload["origin"])` inside a handler read as the module
        constant and passed; the clause now rejects any name bound anywhere in
        the function body;
    (3) a `<record>.origin` read whose root is not a parameter — echoing back
        the origin a stored record already carries.

    Anything else — a subscript, a call result, a conditional, a literal string,
    a parameter — is an offender, whether or not anyone has thought of it yet.
    All six existing call sites already satisfy this, so the inversion costs
    nothing today and constrains everything written tomorrow.

    **What this test does and does not prove.** Clause (3) reads a `.origin`
    attribute and cannot tell a stored record from an object built in the
    handler out of the request body: `parsed = Note.model_validate(await
    request.json())` followed by `origin=parsed.origin` satisfies it. That
    distinction is not available to an AST — it is the same limitation as the
    `getattr` hole documented on
    `test_only_one_module_may_write_an_approval_decision`, and widening the
    rule only moves the boundary. The guarantee against a hand-parsed body is
    the behavioural origin tests — `test_the_agent_router_never_sets_a_human_
    origin` and the timeline assertions in the agent-surface gate tests — which
    assert against the *stored event* however its origin was spelled.
    """
    checked = 0
    offenders: list[str] = []
    for path in _api_modules():
        tree = ast.parse(path.read_text())
        constants = _module_level_names(tree)
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            args = func.args
            params = {
                a.arg
                for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
                if a is not None
            }
            bound = _bound_names_in(func)
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg != "origin":
                        continue
                    checked += 1
                    value = kw.value
                    root = _root_name(value)
                    enum_member = (
                        isinstance(value, ast.Attribute)
                        and isinstance(value.value, ast.Name)
                        and value.value.id == "Origin"
                    )
                    constant = (
                        isinstance(value, ast.Name)
                        and value.id in constants
                        and value.id not in bound
                    )
                    record_read = (
                        isinstance(value, ast.Attribute)
                        and value.attr == "origin"
                        and root is not None
                        and root not in params
                    )
                    if not (enum_member or constant or record_read):
                        offenders.append(
                            f"{path.name}:{node.lineno}: origin={ast.unparse(value)} is not an "
                            "Origin member, a module constant, or a stored record's origin"
                        )

    assert checked, "no origin= keyword was inspected at all — the scan found nothing"
    assert not offenders, "origin is not server-derived:\n" + "\n".join(offenders)


def test_the_agent_router_derives_origin_from_a_module_constant() -> None:
    """Every event the agent router writes must take its origin from the
    module-level ORIGIN, never from a name that could reach it from a request."""
    path = SRC / "mcpforge" / "api" / "agent.py"
    tree = ast.parse(path.read_text())

    assigned = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "ORIGIN" for t in node.targets)
    ]
    assert len(assigned) == 1, "agent.py must define exactly one module-level ORIGIN"
    value = assigned[0].value
    assert isinstance(value, ast.Attribute) and value.attr == "AGENT", (
        "the agent router's ORIGIN must be Origin.AGENT"
    )

    # No call anywhere in the module may pass origin=<anything but ORIGIN>.
    offenders = [
        f"agent.py:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "origin" and not (isinstance(kw.value, ast.Name) and kw.value.id == "ORIGIN")
    ]
    assert not offenders, "origin passed from something other than ORIGIN:\n" + "\n".join(offenders)


def test_the_agent_router_never_sets_a_human_origin() -> None:
    path = SRC / "mcpforge" / "api" / "agent.py"
    tree = ast.parse(path.read_text())
    offenders = [
        f"agent.py:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "HUMAN"
    ]
    assert not offenders, "agent router references Origin.HUMAN:\n" + "\n".join(offenders)


def test_approval_creation_has_exactly_one_implementation() -> None:
    """The agent path and the human path must converge. Two constructors of
    `Approval` in the API layer would mean an agent-only path could drift."""
    offenders: list[str] = []
    for path in _api_modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Approval"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == ["approvals.py:" + offenders[0].split(":")[1]], (
        "Approval is constructed in more than one place in the API layer: " + ", ".join(offenders)
    )
