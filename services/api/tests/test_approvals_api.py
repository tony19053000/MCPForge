"""Approval endpoints — F2-05. The gate mechanism, tested as a security control."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mcpforge.auth.identity import AuthError, VerifiedIdentity
from mcpforge.config import Settings
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.main import create_app
from mcpforge.models.core import artifact_hash
from mcpforge.store.memory import InMemoryStore

OWNER = "uid-owner"
OTHER = "uid-stranger"

PLAN = {"tools": ["search_hotels", "prepare_booking"]}
PLAN_HASH = artifact_hash(PLAN)


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


def request_approval(client: TestClient, session_id: str, uid: str = OWNER) -> dict[str, object]:
    response = client.post(
        f"/api/sessions/{session_id}/approvals",
        json={"gate": "TOOL_PLAN", "artifact_hash": PLAN_HASH, "summary": "2 tools"},
        headers=auth(uid),
    )
    assert response.status_code == 201
    return dict(response.json())


# -- the basic contract ----------------------------------------------------


def test_an_approval_starts_pending_with_no_actor(client: TestClient) -> None:
    approval = request_approval(client, make_session(client))
    assert approval["status"] == "PENDING"
    assert approval["actor_uid"] is None


def test_a_gate_is_closed_until_a_human_decides(client: TestClient) -> None:
    session_id = make_session(client)
    request_approval(client, session_id)
    gate = client.get(
        f"/api/sessions/{session_id}/gate",
        params={"gate": "TOOL_PLAN", "artifact_hash": PLAN_HASH},
        headers=auth(),
    ).json()
    assert gate["open"] is False


def test_approving_opens_the_gate_and_records_who_decided(client: TestClient) -> None:
    session_id = make_session(client)
    approval = request_approval(client, session_id)

    decided = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    ).json()
    assert decided["status"] == "APPROVED"
    assert decided["actor_uid"] == OWNER
    assert decided["decided_at"] is not None

    gate = client.get(
        f"/api/sessions/{session_id}/gate",
        params={"gate": "TOOL_PLAN", "artifact_hash": PLAN_HASH},
        headers=auth(),
    ).json()
    assert gate["open"] is True


def test_rejecting_leaves_the_gate_closed(client: TestClient) -> None:
    session_id = make_session(client)
    approval = request_approval(client, session_id)
    client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "REJECTED"},
        headers=auth(),
    )
    gate = client.get(
        f"/api/sessions/{session_id}/gate",
        params={"gate": "TOOL_PLAN", "artifact_hash": PLAN_HASH},
        headers=auth(),
    ).json()
    assert gate["open"] is False


# -- the rules that make it a security control -----------------------------


def test_regenerating_the_artifact_closes_the_gate_again(client: TestClient) -> None:
    """The heart of it: approving a plan does not approve a different plan."""
    session_id = make_session(client)
    approval = request_approval(client, session_id)
    client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    )

    changed = artifact_hash({"tools": ["search_hotels", "prepare_booking", "cancel_booking"]})
    gate = client.get(
        f"/api/sessions/{session_id}/gate",
        params={"gate": "TOOL_PLAN", "artifact_hash": changed},
        headers=auth(),
    ).json()
    assert gate["open"] is False


def test_approving_one_gate_does_not_open_another(client: TestClient) -> None:
    session_id = make_session(client)
    approval = request_approval(client, session_id)
    client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    )
    gate = client.get(
        f"/api/sessions/{session_id}/gate",
        params={"gate": "PATCH", "artifact_hash": PLAN_HASH},
        headers=auth(),
    ).json()
    assert gate["open"] is False


def test_a_stranger_cannot_decide_your_approval(client: TestClient) -> None:
    session_id = make_session(client)
    approval = request_approval(client, session_id)
    response = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(OTHER),
    )
    assert response.status_code == 404


def test_a_stranger_cannot_request_an_approval_on_your_session(client: TestClient) -> None:
    session_id = make_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/approvals",
        json={"gate": "PATCH", "artifact_hash": PLAN_HASH, "summary": "x"},
        headers=auth(OTHER),
    )
    assert response.status_code == 404


def test_an_approval_cannot_be_decided_twice(client: TestClient) -> None:
    session_id = make_session(client)
    approval = request_approval(client, session_id)
    first = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "REJECTED"},
        headers=auth(),
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    )
    assert second.status_code == 409


def test_a_decision_must_be_terminal(client: TestClient) -> None:
    session_id = make_session(client)
    approval = request_approval(client, session_id)
    response = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "PENDING"},
        headers=auth(),
    )
    assert response.status_code == 422


def test_the_actor_comes_from_the_token_not_the_request_body(client: TestClient) -> None:
    """A caller cannot approve as someone else by saying so."""
    session_id = make_session(client)
    approval = request_approval(client, session_id)
    decided = client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "APPROVED", "actor_uid": "uid-someone-else"},
        headers=auth(),
    ).json()
    assert decided["actor_uid"] == OWNER


def test_deciding_requires_authentication(client: TestClient) -> None:
    session_id = make_session(client)
    approval = request_approval(client, session_id)
    response = client.post(f"/api/approvals/{approval['id']}/decide", json={"decision": "APPROVED"})
    assert response.status_code == 401


# -- activity --------------------------------------------------------------


def test_the_decision_is_recorded_in_the_timeline_as_human_origin(client: TestClient) -> None:
    session_id = make_session(client)
    approval = request_approval(client, session_id)
    client.post(
        f"/api/approvals/{approval['id']}/decide",
        json={"decision": "APPROVED"},
        headers=auth(),
    )
    events = client.get(f"/api/sessions/{session_id}/events", headers=auth()).json()
    kinds = [e["kind"] for e in events]
    assert "approval.requested" in kinds
    decided = next(e for e in events if e["kind"] == "approval.decided")
    assert decided["origin"] == "HUMAN"
