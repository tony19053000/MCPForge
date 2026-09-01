"""Chat and project API — F2-03."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mcpforge.auth.identity import AuthError, VerifiedIdentity
from mcpforge.config import Settings
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.gemini.provider import GeminiTransportError
from mcpforge.main import create_app
from mcpforge.store.memory import InMemoryStore

OWNER = "uid-owner"
OTHER = "uid-stranger"


class TokenIsUid:
    """Bearer token is the uid. Keeps these tests about the API, not about JWTs,
    which have their own suite."""

    async def verify(self, raw_token: str) -> VerifiedIdentity:
        if not raw_token.startswith("uid-"):
            raise AuthError("bad token")
        return VerifiedIdentity(subject=raw_token, issuer="test")


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def gemini() -> FakeGeminiProvider:
    return FakeGeminiProvider(["I found seven workflows in your application"])


@pytest.fixture
def client(
    settings: Settings, store: InMemoryStore, gemini: FakeGeminiProvider
) -> Iterator[TestClient]:
    app = create_app(settings, token_verifier=TokenIsUid(), store=store, gemini=gemini)
    with TestClient(app) as c:
        yield c


def auth(uid: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def make_session(client: TestClient, uid: str = OWNER) -> str:
    project = client.post("/api/projects", json={"name": "hotel"}, headers=auth(uid)).json()
    return str(
        client.post(f"/api/projects/{project['id']}/sessions", headers=auth(uid)).json()["id"]
    )


def read_sse(text: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], lines.get("data", "")))
    return events


# -- ownership -------------------------------------------------------------


def test_a_project_is_owned_by_the_verified_token_not_the_request(client: TestClient) -> None:
    created = client.post("/api/projects", json={"name": "hotel"}, headers=auth()).json()
    assert client.get("/api/projects", headers=auth(OTHER)).json() == []
    assert client.get(f"/api/projects/{created['id']}", headers=auth(OTHER)).status_code == 404


def test_a_new_project_is_read_only_and_a_demo(client: TestClient) -> None:
    body = client.post("/api/projects", json={"name": "hotel"}, headers=auth()).json()
    assert body["access_mode"] == "READ_ONLY"
    assert body["is_demo"] is True


def test_unauthenticated_requests_are_rejected(client: TestClient) -> None:
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/projects", json={"name": "x"}).status_code == 401


def test_a_stranger_cannot_chat_into_someone_elses_session(client: TestClient) -> None:
    session_id = make_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/chat", json={"message": "hi"}, headers=auth(OTHER)
    )
    assert response.status_code == 404


# -- streaming -------------------------------------------------------------


def test_chat_streams_deltas_and_completes(client: TestClient) -> None:
    session_id = make_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/chat", json={"message": "analyze it"}, headers=auth()
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = read_sse(response.text)
    kinds = [name for name, _ in events]
    assert kinds[0] == "turn"
    assert "activity" in kinds
    assert "delta" in kinds
    assert kinds[-1] == "done"

    text = "".join(
        __import__("json").loads(data)["text"] for name, data in events if name == "delta"
    )
    assert "seven workflows" in text


def test_the_assistant_reply_is_persisted(client: TestClient) -> None:
    session_id = make_session(client)
    client.post(f"/api/sessions/{session_id}/chat", json={"message": "analyze it"}, headers=auth())
    turns = client.get(f"/api/sessions/{session_id}/turns", headers=auth()).json()
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert "seven workflows" in turns[1]["text"]


def test_history_is_sent_to_the_model(client: TestClient, gemini: FakeGeminiProvider) -> None:
    session_id = make_session(client)
    client.post(f"/api/sessions/{session_id}/chat", json={"message": "first"}, headers=auth())
    assert gemini.calls[0].messages[-1].text == "first"


def test_the_stream_never_carries_model_reasoning(client: TestClient) -> None:
    """04_FRONTEND_SPEC.md §3 — task summaries only, never chain-of-thought."""
    session_id = make_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/chat", json={"message": "hi"}, headers=auth()
    )
    for name, data in read_sse(response.text):
        if name == "activity":
            assert "thought" not in data.lower()
            assert "reasoning" not in data.lower()


def test_a_model_failure_reports_the_real_error(settings: Settings) -> None:
    failing = FakeGeminiProvider([GeminiTransportError("429 rate limited", retryable=True)])
    app = create_app(settings, token_verifier=TokenIsUid(), store=InMemoryStore(), gemini=failing)
    with TestClient(app) as client:
        session_id = make_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/chat", json={"message": "hi"}, headers=auth()
        )
        errors = [data for name, data in read_sse(response.text) if name == "error"]
        assert errors and "429" in errors[0]


def test_chat_is_unavailable_when_gemini_is_unconfigured(settings: Settings) -> None:
    """503, not a fabricated reply."""
    unconfigured = FakeGeminiProvider([], configured=False)
    app = create_app(
        settings, token_verifier=TokenIsUid(), store=InMemoryStore(), gemini=unconfigured
    )
    with TestClient(app) as client:
        session_id = make_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/chat", json={"message": "hi"}, headers=auth()
        )
        assert response.status_code == 503


def test_an_empty_message_is_rejected(client: TestClient) -> None:
    session_id = make_session(client)
    response = client.post(f"/api/sessions/{session_id}/chat", json={"message": ""}, headers=auth())
    assert response.status_code == 422
