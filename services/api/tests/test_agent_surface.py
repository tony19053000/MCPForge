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
from mcpforge.models.core import Artifact, ArtifactKind
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


#: Every mutation tool, with a body that passes validation. Used by the sweeps
#: below so a newly added tool cannot quietly skip the gate.
MUTATION_TOOLS: list[tuple[str, dict[str, object]]] = [
    ("repository", {"repository_full_name": "acme/site", "branch": "main"}),
    ("workflows", {"workflow_ids": ["searchRooms"]}),
]


# -- F7-02 read tools -------------------------------------------------------


def test_an_unauthenticated_agent_gets_an_error_not_data(client: TestClient) -> None:
    session_id = make_session(client)
    response = client.get(f"/api/agent/sessions/{session_id}/status")
    assert response.status_code == 401
    assert "name" not in response.text


def test_an_agent_cannot_read_another_users_session(client: TestClient) -> None:
    session_id = make_session(client, OWNER)
    for path in ("status", "workflows", "plan", "validation"):
        response = client.get(f"/api/agent/sessions/{session_id}/{path}", headers=auth(OTHER))
        assert response.status_code == 404, path


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


def test_a_pull_request_request_does_not_rewrite_the_patch_artifact(
    client: TestClient,
) -> None:
    """Requesting a PR must leave the patch artifact byte-identical.

    Rewriting it would change the patch's derived hash, so the approval the
    human just gave would stop covering the patch that gets written — silently,
    at write time. Asserted on the stored artifact, not on the gate: the gate is
    queried with a caller-supplied hash and would still answer "open" for the
    old one.
    """
    session_id = make_session(client)
    seed_plan(client, session_id)
    patch = client.post(
        f"/api/agent/sessions/{session_id}/patch", json={"summary": "generate"}, headers=auth()
    ).json()
    client.post(
        f"/api/approvals/{patch['approval_id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    )

    def stored_patch_hash() -> str:
        body = client.get(f"/api/agent/sessions/{session_id}/plan", headers=auth())
        assert body.status_code == 200
        store = client.app.state.store  # type: ignore[attr-defined]
        artifact = asyncio.run(store.get_artifact(session_id, ArtifactKind.PATCH, OWNER))
        assert artifact is not None
        return str(artifact.hash)

    before = stored_patch_hash()
    assert before == patch["artifact_hash"]

    client.post(
        f"/api/agent/sessions/{session_id}/pull-request",
        json={"title": "Add WebMCP tools"},
        headers=auth(),
    )
    assert stored_patch_hash() == before, "the PR request overwrote the approved patch"

    gate = client.get(
        f"/api/sessions/{session_id}/gate",
        params={"gate": "PATCH", "artifact_hash": stored_patch_hash()},
        headers=auth(),
    ).json()
    assert gate["open"] is True


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
