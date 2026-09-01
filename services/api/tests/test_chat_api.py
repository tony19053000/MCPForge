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


class ExplicitCloseStream:
    """An async *iterator*, deliberately not an async generator.

    Python tears down async generators by itself. That is why an earlier version
    of this test passed with the handler's cleanup deleted: the fake's `finally`
    ran during garbage collection, not because the handler did anything.

    This class records closure only when `aclose()` is actually awaited, so the
    handler's `finally` block is the only thing that can set it.
    """

    def __init__(self, chunk_count: int) -> None:
        self.chunk_count = chunk_count
        self.yielded = 0
        self.closed = False

    def __aiter__(self) -> ExplicitCloseStream:
        return self

    async def __anext__(self) -> str:
        if self.yielded >= self.chunk_count:
            raise StopAsyncIteration
        self.yielded += 1
        return f"chunk-{self.yielded} "

    async def aclose(self) -> None:
        self.closed = True


class CountingStreamProvider:
    """Hands out one ExplicitCloseStream so the test can inspect it afterwards."""

    def __init__(self, chunk_count: int = 5000) -> None:
        self.stream = ExplicitCloseStream(chunk_count)

    @property
    def configured(self) -> bool:
        return True

    @property
    def model(self) -> str:
        return "fake"

    async def generate_structured(self, request: object, schema: object) -> object:
        raise NotImplementedError

    def stream_text(self, request: object) -> ExplicitCloseStream:
        return self.stream


def _app_with(provider: CountingStreamProvider, settings: Settings):  # type: ignore[no-untyped-def]
    return create_app(
        settings,
        token_verifier=TokenIsUid(),
        store=InMemoryStore(),
        gemini=provider,  # type: ignore[arg-type]
    )


def test_the_handler_closes_the_upstream_stream_itself(settings: Settings) -> None:
    """The `finally: await stream_iter.aclose()` in chat.py is what closes it.

    ExplicitCloseStream is not an async generator, so nothing in the runtime
    will close it on our behalf. If the handler's cleanup is removed, this fails.
    """
    provider = CountingStreamProvider(chunk_count=3)
    with TestClient(_app_with(provider, settings)) as client:
        session_id = make_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/chat", json={"message": "hi"}, headers=auth()
        )
        assert response.status_code == 200
    assert provider.stream.closed is True


def test_the_handler_stops_pulling_from_the_model_on_disconnect(settings: Settings) -> None:
    """`if await request.is_disconnected(): break` is what stops the loop.

    The ASGI app is driven directly so the disconnect is deterministic: the
    receive channel yields the request body once, then http.disconnect forever.
    Without the check, the handler drains all 5000 chunks.
    """
    import anyio

    provider = CountingStreamProvider(chunk_count=5000)
    app = _app_with(provider, settings)

    async def drive() -> None:
        # Create the session through the normal client first.
        with TestClient(app) as client:
            session_id = make_session(client)

        body = b'{"message":"long answer please"}'
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/api/sessions/{session_id}/chat",
            "raw_path": f"/api/sessions/{session_id}/chat".encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"authorization", f"Bearer {OWNER}".encode()),
            ],
            "client": ("test", 1234),
            "server": ("testserver", 80),
        }

        sent_body = False

        async def receive() -> dict[str, object]:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            return None

        await app(scope, receive, send)

    anyio.run(drive)

    # Lower bound too, so the test cannot pass by the stream never running.
    assert 0 < provider.stream.yielded < 50, (
        f"handler pulled {provider.stream.yielded} chunks from the model after the "
        "client disconnected; the is_disconnected() check is not stopping the loop"
    )


def test_activity_events_are_emitted_so_the_no_reasoning_check_is_not_vacuous(
    client: TestClient,
) -> None:
    """Guards the test below: if no activity event were ever sent, it would pass
    for the wrong reason."""
    session_id = make_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/chat", json={"message": "hi"}, headers=auth()
    )
    activity = [data for name, data in read_sse(response.text) if name == "activity"]
    assert len(activity) >= 2


def test_no_stream_event_carries_a_reasoning_shaped_field(client: TestClient) -> None:
    """04_FRONTEND_SPEC.md §3 — the wire carries task summaries, never reasoning."""
    session_id = make_session(client)
    response = client.post(
        f"/api/sessions/{session_id}/chat", json={"message": "hi"}, headers=auth()
    )
    banned = ("thought", "thinking_", "reasoning", "chain_of_thought", "rationale", "scratchpad")
    for name, data in read_sse(response.text):
        if name == "delta":
            continue  # the model's visible answer, which is the point
        lowered = data.lower()
        for word in banned:
            assert word not in lowered, f"{name} event carried {word!r}: {data}"


class FailingStream(ExplicitCloseStream):
    """Yields a couple of chunks, then fails like a real upstream would."""

    def __init__(self, raise_after: int = 2) -> None:
        super().__init__(chunk_count=1000)
        self._raise_after = raise_after

    async def __anext__(self) -> str:
        if self.yielded >= self._raise_after:
            raise GeminiTransportError("503 upstream died", retryable=True)
        return await super().__anext__()


class FailingMidStreamProvider:
    def __init__(self) -> None:
        self.stream = FailingStream()

    @property
    def configured(self) -> bool:
        return True

    @property
    def model(self) -> str:
        return "fake"

    async def generate_structured(self, request: object, schema: object) -> object:
        raise NotImplementedError

    def stream_text(self, request: object) -> ExplicitCloseStream:
        return self.stream


def test_the_stream_is_closed_even_when_the_model_fails_mid_response(
    settings: Settings,
) -> None:
    """Covers the error path, where an `else:` would silently skip cleanup."""
    provider = FailingMidStreamProvider()
    app = create_app(
        settings,
        token_verifier=TokenIsUid(),
        store=InMemoryStore(),
        gemini=provider,  # type: ignore[arg-type]
    )
    with TestClient(app) as client:
        session_id = make_session(client)
        response = client.post(
            f"/api/sessions/{session_id}/chat", json={"message": "hi"}, headers=auth()
        )
        errors = [data for name, data in read_sse(response.text) if name == "error"]
        assert errors and "503" in errors[0]
    assert provider.stream.closed is True
