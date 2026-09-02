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
from fastapi import APIRouter, WebSocket
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mcpforge.api import agent as agent_module
from mcpforge.auth.identity import AuthError, VerifiedIdentity
from mcpforge.config import Settings
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.main import create_app
from mcpforge.models.core import Artifact, ArtifactKind, artifact_hash
from mcpforge.models.patch import ChangeKind, FileChange, GeneratedPatch
from mcpforge.store.memory import InMemoryStore
from tests.structure import SRC

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


#: Every gate-opening endpoint, with a body that passes validation. These lists
#: are hand-maintained, so `test_every_agent_post_route_is_swept` enumerates the
#: router and fails when a route appears in neither — otherwise a newly added
#: endpoint would be swept by nothing while the sweeps read as exhaustive.
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

#: The agent router speaks only these two verbs. Restricting the vocabulary is
#: what lets the sweeps below be keyed on path: a PATCH added to a path a POST
#: already covers would otherwise be "swept" while the verb was not.
ALLOWED_METHODS = frozenset({"get", "post"})


class UnexpectedRouteError(AssertionError):
    """A leaf on the agent router that this file does not know how to classify."""


def routes_of(router: APIRouter) -> list[tuple[str, str]]:
    """Every (method, path) in a router tree, including nested includes.

    Walked from the router object, not from `app.routes` filtered by path
    prefix. Two rounds were lost to that filter: FastAPI stores a nested
    `include_router`'s children with their **unprefixed** path, so
    `startswith("/api/agent")` discarded them and a POST added through one extra
    `include_router` line was in no bucket while 55 tests stayed green.

    Unknown leaf types raise rather than being skipped. An `APIWebSocketRoute`
    has a path, no HTTP verb and no `.routes`, so a skip-what-you-do-not-
    recognise walker dropped it silently — and a WebSocket endpoint that
    approved its own gates passed every sweep in this file.
    """
    found: list[tuple[str, str]] = []
    for route in router.routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            found.extend(routes_of(nested))
            continue
        if isinstance(route, APIRouter):
            found.extend(routes_of(route))
            continue
        if not isinstance(route, APIRoute):
            raise UnexpectedRouteError(
                f"{type(route).__name__} at {getattr(route, 'path', '?')} is on the agent "
                "router. Every agent-reachable route must be an APIRoute this file can "
                "classify — a WebSocket or Mount is an unswept path around every gate."
            )
        found.extend(
            (method.lower(), _suffix(route.path))
            for method in route.methods or set()
            if method.upper() not in {"HEAD", "OPTIONS"}
        )
    return found


def _suffix(path: str) -> str:
    """The part that identifies the endpoint, however it was mounted.

    Top-level routes carry the router's `/api/agent` prefix; children of a
    nested include do not. Both normalise to the same key.
    """
    return path.removeprefix("/api/agent").removeprefix("/sessions/{session_id}/")


def served_agent_routes(client: TestClient) -> set[tuple[str, str]]:
    """Every (method, suffix) the **app** actually serves under `/api/agent`.

    Read from each mounted router's effective route contexts, whose `.path` is
    the served path — so a route reaches this set however it was mounted, and
    `include_in_schema=False` cannot hide it the way it hid from `openapi()`.

    This exists because rooting the enumeration at `agent_module.router` alone
    traded away coverage the previous app-level walk had: a second router with
    its own `/api/agent` prefix, included in `main.py`, served a self-approving
    POST that the router walk could not see and 773 tests stayed green.
    """
    found: set[tuple[str, str]] = set()
    for route in client.app.routes:  # type: ignore[attr-defined]
        contexts = route.effective_candidates() if hasattr(route, "effective_candidates") else []
        for context in contexts:
            path = getattr(context, "path", "")
            if not path.startswith("/api/agent"):
                continue
            methods = getattr(context, "methods", None)
            if not methods:
                raise UnexpectedRouteError(
                    f"a route with no HTTP method is served at {path}. A WebSocket or "
                    "Mount under /api/agent is an unswept path around every gate."
                )
            found.update(
                (method.lower(), _suffix(path))
                for method in methods
                if method.upper() not in {"HEAD", "OPTIONS"}
            )
    return found


def agent_routes(client: TestClient) -> list[tuple[str, str]]:
    """The agent surface.

    Two independent readings, required to agree:

    - `routes_of(agent_module.router)` walks the router that defines the surface,
      which is what catches a route added through a nested `include_router`
      (its children are stored unprefixed, so a path filter misses them).
    - `served_agent_routes(client)` reads what the app actually serves, which is
      what catches a route reaching `/api/agent` from a different router.

    Each reading has been wrong on its own, in different ways, one round apart.
    Requiring both to agree is what makes a route hard to hide: it must be
    absent from the defining router *and* absent from the served app.
    """
    declared = set(routes_of(agent_module.router))
    served = served_agent_routes(client)

    assert declared == served, (
        "the agent surface disagrees with what the app serves.\n"
        f"served but not on agent.router: {sorted(served - declared)}\n"
        f"on agent.router but not served: {sorted(declared - served)}"
    )
    return sorted(declared)


def test_the_route_enumeration_actually_finds_the_router(client: TestClient) -> None:
    """A guard on the guards.

    Every sweep in this file is built on `agent_routes`. If it returns nothing —
    as it did when it scanned only the top level of `app.routes` — every one of
    them passes while checking nothing.
    """
    found = agent_routes(client)
    assert len(found) >= 12, f"agent_routes found only {len(found)} routes: {found}"
    assert ("post", "pull-request") in found
    assert ("get", "status") in found


def test_the_route_enumeration_sees_a_nested_include() -> None:
    """A POST added through one extra `include_router` line was in no bucket
    while the whole suite stayed green, because the child's stored path has no
    `/api/agent` prefix to match on."""
    outer, inner = APIRouter(prefix="/api/agent"), APIRouter()

    @inner.post("/sessions/{session_id}/deploy")
    async def deploy(session_id: str) -> dict[str, str]:
        return {}

    outer.include_router(inner)
    assert ("post", "deploy") in routes_of(outer)


def test_the_route_enumeration_refuses_a_route_it_cannot_classify() -> None:
    """A WebSocket has a path, no HTTP verb and no `.routes`. Skipping it left
    an agent-only path around every gate invisible to every sweep here."""
    router = APIRouter(prefix="/api/agent")

    @router.websocket("/sessions/{session_id}/stream")
    async def stream(websocket: WebSocket, session_id: str) -> None:
        await websocket.close()

    with pytest.raises(UnexpectedRouteError, match="WebSocket"):
        routes_of(router)


def test_the_agent_router_speaks_only_get_and_post(client: TestClient) -> None:
    offenders = [
        f"{method.upper()} {path}"
        for method, path in agent_routes(client)
        if method not in ALLOWED_METHODS
    ]
    assert not offenders, (
        "the agent router must use only GET and POST, so the path-keyed sweeps are "
        "complete. Found: " + ", ".join(offenders)
    )


def test_every_agent_route_is_classified(client: TestClient) -> None:
    """No route of any verb reaches the agent surface unclassified.

    Both axes, from what is mounted rather than from what is documented. Adding
    a route means putting it in a bucket, and each bucket has tests attached:
    MUTATION_TOOLS must open a gate, STAGE_TOOLS must not, READ_TOOLS must
    neither write nor decide.
    """
    prefix = "/api/agent/sessions/{session_id}/"
    classified = {("post", p) for p, _ in MUTATION_TOOLS + STAGE_TOOLS} | {
        ("get", p) for p in READ_TOOLS
    }

    unclassified = {
        (method, path.removeprefix(prefix)) for method, path in agent_routes(client)
    } - classified

    assert not unclassified, (
        "agent routes in no bucket: "
        + ", ".join(f"{m.upper()} {p}" for m, p in sorted(unclassified))
        + ". Add each to MUTATION_TOOLS (opens a gate), STAGE_TOOLS (records only) "
        "or READ_TOOLS (reads only)."
    )


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
    response = client.get(f"/api/agent/sessions/{session_id}/{path}", headers=auth())
    assert response.status_code == 200, path
    after = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()

    assert len(after) == len(before), f"GET {path} wrote {len(after) - len(before)} event(s)"
    approval = client.get(f"/api/approvals/{asked['approval_id']}", headers=auth()).json()
    assert approval["status"] == "PENDING", f"GET {path} decided an approval"
    assert approval["actor_uid"] is None


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


def test_no_request_model_declares_an_origin_field() -> None:
    """If a request body could carry an origin, FastAPI would bind it and the
    server-side derivation would be bypassed. Parsed, not grepped: comments and
    docstrings explaining this rule must not satisfy it."""
    offenders: list[str] = []
    for path in _api_modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            if "BaseModel" not in bases:
                continue
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id == "origin"
                    and node.name.endswith("Body")
                ):
                    offenders.append(f"{path.name}:{node.name}.origin")
    assert not offenders, "request body carries a caller-settable origin:\n" + "\n".join(offenders)


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
