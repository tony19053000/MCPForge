"""F8-02 — the attestation path: launcher → workload → bucket → relying party.

**What these tests exercise, and what they do not.** Every production function
on the path runs unmodified:

- the launcher client, against a local Unix-socket server implementing the
  launcher's documented contract;
- the workload's delivery code, against `FakeGoogle`, a local HTTP stand-in for
  the metadata server's access-token endpoint and the Cloud Storage JSON API;
- the relying party's GCS reader, against the same stand-in, and its
  verification, through F8-01's `verify_attestation_token` with F8-01's test key.

The stand-ins are transport. The tokens are minted only by F8-01's
`make_attestation_token`, and the test key it uses is unreachable from
production (`test_production_code_cannot_reach_the_test_key`). Nothing here is
attestation, simulated or otherwise: F8-02 completes only when a real
Confidential Space run produces a token that the API verifies against its own
pinned digest and issued nonce, and until then it is `BLOCKED`.
"""

from __future__ import annotations

import ast
import hashlib
import http.server
import json
import logging
import os
import shutil
import socket
import socketserver
import stat
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import structlog
from cryptography.hazmat.primitives.asymmetric import rsa

import tests.test_attestation as f8_01
from mcpforge.config import SecureExecutorKind, Settings
from mcpforge.execution import confidential_space
from mcpforge.execution.attestation import AttestationFailure, AttestationPolicyError, TrustLevel
from mcpforge.execution.confidential_space import (
    MAX_TOKEN_BYTES,
    RUN_TTL,
    AttestationRequiredError,
    ConfidentialSpaceKeyResolver,
    ConfidentialSpaceSecureExecutor,
    DeliveryUnreadableError,
    NoAttestedJobRunnerError,
    RunVerificationFailure,
    TokenRetrievalError,
    TokenRetrievalFailure,
    check_launcher_audience,
    request_attestation_token,
    run_object_name,
    verify_delivered_run,
)
from mcpforge.execution.provider import Command, SecureExecutionProvider, Workspace, WorkspaceSpec
from mcpforge.execution.token_delivery import (
    DeliveryError,
    DeliveryFailure,
    deliver_token,
    fetch_access_token,
)
from mcpforge.relying_party.__main__ import main as relying_party_cli
from mcpforge.relying_party.gcs import GcsDeliveredTokenSource
from mcpforge.relying_party.runs import FileAttestationRunStore
from tests.launch_plan import ENTRYPOINT, load_entrypoint
from tests.structure import SRC, call_sites, files_importing, imported_modules, python_files
from tests.test_trust_api import auth, build_client, make_session, trust_state

#: F8-01's token-minting fixture, reused rather than re-implemented. Assigned
#: rather than imported so pytest registers it without a lint rule mistaking the
#: fixture parameter for a shadowed import.
make_attestation_token = f8_01.make_attestation_token

#: The digest of the image that "ran" — what a token names.
RUNNING_DIGEST = f8_01.IMAGE_DIGEST
#: A digest the API could be configured with instead.
OTHER_DIGEST = f8_01.OTHER_DIGEST
WORKLOAD_SA = f8_01.WORKLOAD_SA
BUCKET = "mcpforge-aa5c2-attestation"
AUDIENCE = "mcpforge-attestation-" + "5c" * 16


# ---------------------------------------------------------------------------
# A local server implementing the launcher's documented contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


Behaviour = Callable[[http.server.BaseHTTPRequestHandler], None]


class _LauncherServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path: str, behaviour: Behaviour) -> None:
        self.behaviour = behaviour
        self.received: list[Received] = []
        super().__init__(path, _LauncherHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        return


class _LauncherHandler(http.server.BaseHTTPRequestHandler):
    server: _LauncherServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        return

    def address_string(self) -> str:
        return "launcher-socket"

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.received.append(
            Received(
                method=self.command,
                path=self.path,
                headers={name.lower(): value for name, value in self.headers.items()},
                body=body,
            )
        )
        self.server.behaviour(self)

    def do_POST(self) -> None:
        self._serve()

    def do_GET(self) -> None:
        self._serve()


def serve_token(body: str | bytes, *, status: int = 200, content_length: bool = True) -> Behaviour:
    data = body.encode("utf-8") if isinstance(body, str) else body

    def behave(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(status)
        if content_length:
            handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    return behave


def serve_raw(data: bytes, *, drip: bytes = b"", interval: float = 0.0) -> Behaviour:
    def behave(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.wfile.write(data)
        for byte in drip:
            time.sleep(interval)
            try:
                handler.wfile.write(bytes([byte]))
            except OSError:
                return

    return behave


def serve_after(delay: float, inner: Behaviour) -> Behaviour:
    def behave(handler: http.server.BaseHTTPRequestHandler) -> None:
        time.sleep(delay)
        inner(handler)

    return behave


def serve_nothing() -> Behaviour:
    def behave(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.close_connection = True

    return behave


Launcher = Callable[[Behaviour], tuple[Path, _LauncherServer]]


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    # AF_UNIX paths are limited to 108 bytes; pytest's tmp_path can exceed that.
    directory = Path(tempfile.mkdtemp(prefix="mcpforge-cs-"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def launcher(socket_dir: Path) -> Iterator[Launcher]:
    servers: list[_LauncherServer] = []

    def start(behaviour: Behaviour) -> tuple[Path, _LauncherServer]:
        path = socket_dir / f"teeserver-{len(servers)}.sock"
        server = _LauncherServer(str(path), behaviour)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        servers.append(server)
        return path, server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def dead_socket(directory: Path) -> Path:
    path = directory / "dead.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()
    return path


def sha256_prefix(token: str) -> str:
    """Computed here, independently of `fingerprint_of`, so the two can disagree."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# FakeGoogle — transport only: the metadata token endpoint and Cloud Storage
# ---------------------------------------------------------------------------

METADATA_TOKEN_PATH = "/computeMetadata/v1/instance/service-accounts/default/token"


class FakeGoogle:
    """A local stand-in for two Google HTTP surfaces, and nothing more.

    `GET` the metadata server's access-token path (only with
    `Metadata-Flavor: Google`); `POST` a simple media upload (create-only with
    `ifGenerationMatch=0`); `GET` an object with `alt=media`. Storage calls need
    the bearer this server issued. Transport, not attestation: it serves back
    exactly the bytes it was given.
    """

    def __init__(self) -> None:
        self.access_token = "ya29.ACCESS-CANARY-" + "q" * 48
        self.objects: dict[tuple[str, str], bytes] = {}
        self.requests: list[Received] = []
        self.upload_status: int | None = None
        self.metadata_body: bytes | None = None
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _record(self) -> tuple[urllib.parse.SplitResult, dict[str, list[str]], bytes]:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                fake.requests.append(
                    Received(
                        self.command,
                        self.path,
                        {k.lower(): v for k, v in self.headers.items()},
                        body,
                    )
                )
                parts = urllib.parse.urlsplit(self.path)
                return parts, urllib.parse.parse_qs(parts.query), body

            def _reply(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {fake.access_token}"

            def do_GET(self) -> None:
                parts, query, _body = self._record()
                if parts.path == METADATA_TOKEN_PATH:
                    if self.headers.get("Metadata-Flavor") != "Google":
                        self._reply(403)
                        return
                    body = fake.metadata_body
                    if body is None:
                        body = json.dumps(
                            {"access_token": fake.access_token, "expires_in": 3599}
                        ).encode()
                    self._reply(200, body)
                    return
                prefix = "/storage/v1/b/"
                if parts.path.startswith(prefix) and query.get("alt") == ["media"]:
                    if not self._authorised():
                        self._reply(401)
                        return
                    bucket, _, name = parts.path[len(prefix) :].partition("/o/")
                    key = (urllib.parse.unquote(bucket), urllib.parse.unquote(name))
                    if key not in fake.objects:
                        self._reply(404)
                        return
                    self._reply(200, fake.objects[key])
                    return
                self._reply(400)

            def do_POST(self) -> None:
                parts, query, body = self._record()
                prefix = "/upload/storage/v1/b/"
                if not (parts.path.startswith(prefix) and parts.path.endswith("/o")):
                    self._reply(400)
                    return
                if not self._authorised():
                    self._reply(401)
                    return
                if fake.upload_status is not None:
                    self._reply(fake.upload_status)
                    return
                bucket = urllib.parse.unquote(parts.path[len(prefix) : -len("/o")])
                key = (bucket, query["name"][0])
                if key in fake.objects and query.get("ifGenerationMatch") == ["0"]:
                    self._reply(412)
                    return
                fake.objects[key] = body
                self._reply(200, json.dumps({"name": key[1]}).encode())

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True).start()
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def metadata_url(self) -> str:
        return self.base + METADATA_TOKEN_PATH

    @property
    def upload_base(self) -> str:
        return self.base + "/upload/storage/v1"

    @property
    def api_base(self) -> str:
        return self.base + "/storage/v1"

    def deliver(self, token: confidential_space.AttestationToken, run_id: str) -> str:
        """The real `deliver_token`, pointed at this stand-in."""
        return deliver_token(
            token,
            bucket=BUCKET,
            run_id=run_id,
            access_token=lambda: fetch_access_token(url=self.metadata_url),
            upload_base=self.upload_base,
        )

    def reader(self) -> GcsDeliveredTokenSource:
        """The real GCS reader, pointed at this stand-in with its bearer."""
        return GcsDeliveredTokenSource(
            BUCKET, credentials=_Credentials(self.access_token), api_base=self.api_base
        )

    def put(self, run_id: str, body: bytes) -> None:
        self.objects[(BUCKET, run_object_name(run_id))] = body

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@dataclass
class _Credentials:
    """ADC-shaped: a bearer and `valid`. Only the token source is replaced."""

    token: str
    valid: bool = True


@pytest.fixture
def google() -> Iterator[FakeGoogle]:
    fake = FakeGoogle()
    yield fake
    fake.close()


# ---------------------------------------------------------------------------
# The relying party's world: a real run store and a transport
# ---------------------------------------------------------------------------


@dataclass
class FakeBucket:
    """An in-memory `DeliveredTokenSource`. Transport only."""

    objects: dict[str, bytes] = field(default_factory=dict)
    fetches: list[str] = field(default_factory=list)
    failure: Exception | None = None

    def fetch(self, object_name: str) -> bytes | None:
        self.fetches.append(object_name)
        if self.failure is not None:
            raise self.failure
        return self.objects.get(object_name)

    def put(self, run_id: str, body: bytes | str) -> None:
        self.objects[run_object_name(run_id)] = body.encode() if isinstance(body, str) else body


@pytest.fixture
def runs(tmp_path: Path) -> FileAttestationRunStore:
    return FileAttestationRunStore(tmp_path / "runs")


def relying_party(
    runs: FileAttestationRunStore,
    tokens: Any,
    rsa_key: rsa.RSAPrivateKey,
    *,
    image_digest: str = RUNNING_DIGEST,
    clock: Callable[[], datetime] | None = None,
) -> ConfidentialSpaceSecureExecutor:
    """The API's executor, configured from the API's values only."""
    return ConfidentialSpaceSecureExecutor(
        runs=runs,
        tokens=tokens,
        image_digest=image_digest,
        workload_service_account=WORKLOAD_SA,
        key_resolver=f8_01._Resolver(rsa_key),
        clock=clock if clock is not None else (lambda: datetime.now(UTC)),
    )


# ---------------------------------------------------------------------------
# 1. The launcher client, against the documented contract
# ---------------------------------------------------------------------------


def test_a_token_is_requested_exactly_as_the_launcher_documents(
    launcher: Launcher, make_attestation_token: f8_01.MakeAttestationToken
) -> None:
    """Method, path, content type and body, as literals from Google's page."""

    token = make_attestation_token(audience=AUDIENCE)
    path, server = launcher(serve_token(token))

    obtained = request_attestation_token(AUDIENCE, socket_path=path, timeout_seconds=5)

    assert obtained.value == token
    assert len(server.received) == 1
    request = server.received[0]
    assert request.method == "POST"
    assert request.path == "/v1/token"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["host"] == "localhost"
    # Exactly these two keys: the launcher decodes with DisallowUnknownFields.
    assert json.loads(request.body) == {"audience": AUDIENCE, "token_type": "OIDC"}


@pytest.mark.parametrize(
    ("audience", "why"),
    [
        ("", "empty"),
        ("é" * 257, "514 bytes — over the 512-byte limit although only 257 characters"),
        ("https://sts.google.com", "reserved for the default STS token"),
    ],
)
def test_the_audience_constraints_are_googles(launcher: Launcher, audience: str, why: str) -> None:
    path, server = launcher(serve_token("a.b.c"))
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(audience, socket_path=path, timeout_seconds=2)
    assert caught.value.failure is TokenRetrievalFailure.INVALID_AUDIENCE, why
    assert server.received == [], f"the launcher was contacted with an audience that is {why}"


def test_a_512_byte_audience_is_accepted(
    launcher: Launcher, make_attestation_token: f8_01.MakeAttestationToken
) -> None:
    audience = "é" * 256
    token = make_attestation_token(audience=audience)
    path, server = launcher(serve_token(token))
    assert request_attestation_token(audience, socket_path=path, timeout_seconds=5).value == token
    assert json.loads(server.received[0].body)["audience"] == audience


def test_a_missing_socket_is_socket_unavailable(socket_dir: Path) -> None:
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(AUDIENCE, socket_path=socket_dir / "absent.sock")
    assert caught.value.failure is TokenRetrievalFailure.SOCKET_UNAVAILABLE


def test_a_socket_nobody_listens_on_is_a_connection_failure(socket_dir: Path) -> None:
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(AUDIENCE, socket_path=dead_socket(socket_dir))
    assert caught.value.failure is TokenRetrievalFailure.CONNECTION_FAILED


def test_a_launcher_that_hangs_up_is_a_connection_failure(launcher: Launcher) -> None:
    path, _server = launcher(serve_nothing())
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(AUDIENCE, socket_path=path, timeout_seconds=5)
    assert caught.value.failure is TokenRetrievalFailure.CONNECTION_FAILED


def test_a_launcher_that_never_answers_times_out_within_the_bound(launcher: Launcher) -> None:
    path, _server = launcher(serve_after(5.0, serve_token("a.b.c")))
    started = time.monotonic()
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(AUDIENCE, socket_path=path, timeout_seconds=0.5)
    elapsed = time.monotonic() - started
    assert caught.value.failure is TokenRetrievalFailure.TIMEOUT
    assert elapsed < 2.0, f"a 0.5 s deadline took {elapsed:.2f} s"


@pytest.mark.parametrize(
    ("phase", "behaviour"),
    [
        (
            "status line and headers",
            serve_raw(b"", drip=b"HTTP/1.0 200 OK\r\nContent-Length: 5\r\n\r\na.b.c", interval=0.1),
        ),
        (
            "body",
            serve_raw(
                b"HTTP/1.0 200 OK\r\nContent-Length: 400\r\n\r\n", drip=b"a" * 400, interval=0.05
            ),
        ),
    ],
)
def test_a_launcher_that_drips_cannot_stretch_the_deadline(
    launcher: Launcher, phase: str, behaviour: Behaviour
) -> None:
    """Each byte arrives inside the 5 s per-operation timeout, so only the
    whole-exchange watchdog can end this; without it the drip runs 4 s / 20 s."""

    path, _server = launcher(behaviour)
    started = time.monotonic()
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(
            AUDIENCE, socket_path=path, timeout_seconds=1.0, operation_timeout_seconds=5.0
        )
    elapsed = time.monotonic() - started
    assert caught.value.failure is TokenRetrievalFailure.TIMEOUT, phase
    assert elapsed < 3.0, f"a 1 s deadline took {elapsed:.2f} s while the {phase} dripped"


def test_a_non_200_is_a_failure_and_its_body_is_not_recorded(
    launcher: Launcher, make_attestation_token: f8_01.MakeAttestationToken
) -> None:
    token = make_attestation_token(audience=AUDIENCE)
    path, _server = launcher(serve_token(token, status=500))
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(AUDIENCE, socket_path=path, timeout_seconds=5)
    assert caught.value.failure is TokenRetrievalFailure.HTTP_STATUS
    assert "500" in caught.value.detail
    assert leaked_fragments(token, str(caught.value)) == []


@pytest.mark.parametrize("declared", [True, False], ids=["declared", "streamed"])
def test_an_oversized_response_is_refused(launcher: Launcher, declared: bool) -> None:
    """Declared: a `Content-Length` over the cap while the server sends less, so
    a client that ignored the header would fail differently. Streamed: no
    length, and a body past the cap."""

    if declared:
        behaviour = serve_raw(
            f"HTTP/1.0 200 OK\r\nContent-Length: {MAX_TOKEN_BYTES + 1}\r\n\r\na.b.c".encode()
        )
    else:
        behaviour = serve_token(b"a" * (MAX_TOKEN_BYTES + 1), content_length=False)
    path, _server = launcher(behaviour)
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(AUDIENCE, socket_path=path, timeout_seconds=5)
    assert caught.value.failure is TokenRetrievalFailure.OVERSIZED_RESPONSE


def test_the_size_cap_is_sixty_four_kibibytes() -> None:
    assert MAX_TOKEN_BYTES == 65536


def test_an_empty_response_is_refused(launcher: Launcher) -> None:
    path, _server = launcher(serve_token(b""))
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(AUDIENCE, socket_path=path, timeout_seconds=5)
    assert caught.value.failure is TokenRetrievalFailure.EMPTY_RESPONSE


def _malformed_bodies(token: str) -> dict[str, Behaviour]:
    return {
        "a JSON wrapper": serve_token(json.dumps({"token": token})),
        "a trailing newline": serve_token(token + "\n"),
        "a leading space": serve_token(" " + token),
        "a CRLF": serve_token(token + "\r\n"),
        "two segments": serve_token(token.rsplit(".", 1)[0]),
        "four segments": serve_token(token + ".extra"),
        "an empty signature": serve_token(token.rsplit(".", 1)[0] + "."),
        "base64 padding": serve_token(token + "=="),
        "non-ASCII": serve_token(token.encode("ascii") + "é".encode()),
        "two tokens": serve_token(token + " " + token),
        "not HTTP at all": serve_raw(b"garbage\r\n\r\n"),
        "an unparseable Content-Length": serve_raw(
            b"HTTP/1.0 200 OK\r\nContent-Length: abc\r\n\r\n" + token.encode("ascii")
        ),
    }


@pytest.mark.parametrize("case", list(_malformed_bodies("a.b.c")))
def test_a_response_that_is_not_exactly_one_compact_jws_is_refused(
    launcher: Launcher, make_attestation_token: f8_01.MakeAttestationToken, case: str
) -> None:
    token = make_attestation_token(audience=AUDIENCE)
    path, _server = launcher(_malformed_bodies(token)[case])
    with pytest.raises(TokenRetrievalError) as caught:
        request_attestation_token(AUDIENCE, socket_path=path, timeout_seconds=5)
    assert caught.value.failure is TokenRetrievalFailure.MALFORMED_RESPONSE, case
    assert leaked_fragments(token, str(caught.value)) == [], case


# ---------------------------------------------------------------------------
# 2. Delivery: the workload writes the raw token, create-only, and that is all
# ---------------------------------------------------------------------------


def test_the_token_is_written_create_only_to_the_run_object(
    google: FakeGoogle, make_attestation_token: f8_01.MakeAttestationToken
) -> None:
    token = confidential_space.AttestationToken(make_attestation_token(audience=AUDIENCE))

    written = google.deliver(token, "cs-test-0001")

    assert written == "gs://mcpforge-aa5c2-attestation/attestation/cs-test-0001.jwt"
    assert google.objects == {(BUCKET, "attestation/cs-test-0001.jwt"): token.value.encode()}
    metadata, upload = google.requests
    assert metadata.method == "GET"
    assert metadata.headers["metadata-flavor"] == "Google"
    assert upload.method == "POST"
    parts = urllib.parse.urlsplit(upload.path)
    assert parts.path == "/upload/storage/v1/b/mcpforge-aa5c2-attestation/o"
    assert urllib.parse.parse_qs(parts.query) == {
        "uploadType": ["media"],
        "name": ["attestation/cs-test-0001.jwt"],
        "ifGenerationMatch": ["0"],
    }
    assert upload.headers["authorization"] == f"Bearer {google.access_token}"
    assert upload.headers["content-type"] == "application/jwt"
    assert upload.body == token.value.encode()


def test_an_existing_object_is_never_overwritten(
    google: FakeGoogle, make_attestation_token: f8_01.MakeAttestationToken
) -> None:
    google.put("cs-test-0001", b"first")
    token = confidential_space.AttestationToken(make_attestation_token(audience=AUDIENCE))
    with pytest.raises(DeliveryError) as caught:
        google.deliver(token, "cs-test-0001")
    assert caught.value.failure is DeliveryFailure.ALREADY_DELIVERED
    assert google.objects[(BUCKET, "attestation/cs-test-0001.jwt")] == b"first"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, DeliveryFailure.UPLOAD_REFUSED), (500, DeliveryFailure.UPLOAD_FAILED)],
)
def test_a_refused_upload_is_a_named_failure(
    google: FakeGoogle, status: int, expected: DeliveryFailure
) -> None:
    google.upload_status = status
    with pytest.raises(DeliveryError) as caught:
        google.deliver(confidential_space.AttestationToken("a.b.c"), "cs-test-0001")
    assert caught.value.failure is expected


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"access_token": ""}'])
def test_no_usable_access_token_is_a_named_failure(google: FakeGoogle, body: bytes) -> None:
    google.metadata_body = body
    with pytest.raises(DeliveryError) as caught:
        google.deliver(confidential_space.AttestationToken("a.b.c"), "cs-test-0001")
    assert caught.value.failure is DeliveryFailure.ACCESS_TOKEN_UNAVAILABLE
    assert google.objects == {}


def test_an_unreachable_metadata_server_is_a_named_failure() -> None:
    with pytest.raises(DeliveryError) as caught:
        fetch_access_token(url="http://127.0.0.1:9/token", timeout=2)
    assert caught.value.failure is DeliveryFailure.ACCESS_TOKEN_UNAVAILABLE


def test_a_run_id_that_is_not_an_object_name_is_refused() -> None:
    for run_id in ("../escape", "a/b", ".hidden", ""):
        with pytest.raises(ValueError):
            run_object_name(run_id)


# ---------------------------------------------------------------------------
# 3. The relying party: issue → deliver → fetch → verify, and every refusal
# ---------------------------------------------------------------------------


async def test_end_to_end_only_the_relying_partys_verification_attests(
    launcher: Launcher,
    google: FakeGoogle,
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    """Issue a run; the real entrypoint requests a token with its audience and
    delivers it; the API's real reader fetches it; the API verifies it; the
    trust panel shows it — and the job is still refused, because nothing
    attested runs jobs."""

    run = runs.issue()
    token = make_attestation_token(audience=run.audience, image_digest=RUNNING_DIGEST)
    socket_path, server = launcher(serve_token(token))

    monkeypatch.setenv("HOME", str(tmp_path))
    jail = tmp_path / "jail"
    jail.mkdir()
    code, record = load_entrypoint().execute(
        {"MCPFORGE_RUN_ID": run.run_id, "MCPFORGE_ATTESTATION_AUDIENCE": run.audience},
        workspace_root=jail,
        socket_path=socket_path,
        deliver=google.deliver,
    )
    assert code == 0, record
    assert record["status"] == "TOKEN_DELIVERED"
    assert "trust_level" not in record["attestation"], "the workload claims nothing"
    assert json.loads(server.received[0].body)["audience"] == run.audience

    executor = relying_party(runs, google.reader(), rsa_key)
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION

    relying = settings.model_copy(update={"secure_executor": SecureExecutorKind.CONFIDENTIAL_SPACE})
    with build_client(relying, executor=executor) as client:
        _, session_id = make_session(client)
        assert trust_state(client, session_id)["secure_execution"]["trust_level"] == (
            "DEVELOPMENT_ISOLATION"
        )
        response = client.post(f"/api/attestation-runs/{run.run_id}/verify", headers=auth())
        assert response.status_code == 200, response.text
        body = response.json()
        panel = trust_state(client, session_id)["secure_execution"]

    assert body["verified"] is True, body
    assert body["trust_level"] == "HARDWARE_ATTESTED"
    assert body["token_sha256_prefix"] == sha256_prefix(token)
    assert panel["trust_level"] == "HARDWARE_ATTESTED"
    assert panel["evidence"]["audience"] == run.audience
    assert panel["evidence"]["image_digest"] == RUNNING_DIGEST
    with pytest.raises(NoAttestedJobRunnerError):
        await executor.create_workspace(WorkspaceSpec(run_id="cs-job"))


async def test_a_run_verifies_once_and_a_replay_is_refused(
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    run = runs.issue()
    bucket = FakeBucket()
    bucket.put(run.run_id, make_attestation_token(audience=run.audience))
    executor = relying_party(runs, bucket, rsa_key)

    first = await executor.verify_run(run.run_id)
    replay = await executor.verify_run(run.run_id)

    assert first.outcome.verified
    assert replay.failure is RunVerificationFailure.RUN_ALREADY_CONSUMED
    assert replay.outcome.evidence is None
    # A replay attempt never keeps, let alone raises, the level.
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


async def test_a_replay_is_refused_before_the_object_is_read(
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    """The consumed check is its own guard, not only the atomic `consume`: a
    verified run is refused from the run record alone, and the bucket is never
    asked for the object again."""

    run = runs.issue()
    bucket = FakeBucket()
    bucket.put(run.run_id, make_attestation_token(audience=run.audience))
    executor = relying_party(runs, bucket, rsa_key)
    assert (await executor.verify_run(run.run_id)).outcome.verified
    assert bucket.fetches == [run_object_name(run.run_id)]

    replay = await executor.verify_run(run.run_id)
    assert replay.failure is RunVerificationFailure.RUN_ALREADY_CONSUMED
    assert bucket.fetches == [run_object_name(run.run_id)], "a replay read the object"
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


def test_two_concurrent_verifiers_cannot_both_consume_a_run(
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    run = runs.issue()
    bucket = FakeBucket()
    bucket.put(run.run_id, make_attestation_token(audience=run.audience))
    barrier = threading.Barrier(8)
    results: list[bool] = []

    def verify() -> None:
        barrier.wait()
        result = verify_delivered_run(
            run.run_id,
            runs=runs,
            tokens=bucket,
            image_digest=RUNNING_DIGEST,
            workload_service_account=WORKLOAD_SA,
            key_resolver=f8_01._Resolver(rsa_key),
        )
        results.append(result.outcome.verified)

    threads = [threading.Thread(target=verify) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 7 + [True]


def _refusal_cases() -> list[str]:
    return [
        "a token for another run's audience",
        "the API pins a different digest",
        "another workload identity",
        "an expired token",
        "the debug image",
        "no object yet",
        "a JSON-wrapped object",
        "an object with a trailing newline",
        "an empty object",
        "an oversized object",
        "an unreadable bucket",
        "a run the API never issued",
        "a run issued too long ago",
        "a run already verified",
    ]


#: Which `RunVerificationFailure` each case must produce, and, for a token the
#: API read and rejected, which F8-01 failure.
EXPECTED: dict[str, tuple[RunVerificationFailure, AttestationFailure | None]] = {
    "a token for another run's audience": (
        RunVerificationFailure.TOKEN_REJECTED,
        AttestationFailure.WRONG_AUDIENCE,
    ),
    "the API pins a different digest": (
        RunVerificationFailure.TOKEN_REJECTED,
        AttestationFailure.UNEXPECTED_IMAGE_DIGEST,
    ),
    "another workload identity": (
        RunVerificationFailure.TOKEN_REJECTED,
        AttestationFailure.UNEXPECTED_WORKLOAD,
    ),
    "an expired token": (RunVerificationFailure.TOKEN_REJECTED, AttestationFailure.EXPIRED),
    "the debug image": (
        RunVerificationFailure.TOKEN_REJECTED,
        AttestationFailure.DEBUG_MODE_ENABLED,
    ),
    "no object yet": (RunVerificationFailure.TOKEN_NOT_DELIVERED, None),
    "a JSON-wrapped object": (RunVerificationFailure.OBJECT_MALFORMED, None),
    "an object with a trailing newline": (RunVerificationFailure.OBJECT_MALFORMED, None),
    "an empty object": (RunVerificationFailure.OBJECT_MALFORMED, None),
    "an oversized object": (RunVerificationFailure.OBJECT_MALFORMED, None),
    "an unreadable bucket": (RunVerificationFailure.DELIVERY_UNREADABLE, None),
    "a run the API never issued": (RunVerificationFailure.UNKNOWN_RUN, None),
    "a run issued too long ago": (RunVerificationFailure.RUN_EXPIRED, None),
    "a run already verified": (RunVerificationFailure.RUN_ALREADY_CONSUMED, None),
}


def test_the_refusal_matrix_covers_every_run_verification_failure() -> None:
    """Derived from the enum: a new failure with no end-to-end case fails."""

    assert set(_refusal_cases()) == set(EXPECTED)
    assert {failure for failure, _ in EXPECTED.values()} == set(RunVerificationFailure)


def arrange(
    case: str,
    runs: FileAttestationRunStore,
    bucket: FakeBucket,
    mint: f8_01.MakeAttestationToken,
) -> tuple[str, str, str]:
    """Set up one refusal; return (run id to verify, configured digest, planted token)."""

    run = runs.issue()
    digest = RUNNING_DIGEST
    token = mint(audience=run.audience)
    body: bytes | str | None = token
    run_id = run.run_id
    if case == "a token for another run's audience":
        other = runs.issue()
        token = mint(audience=other.audience)
        body = token
    elif case == "the API pins a different digest":
        digest = OTHER_DIGEST
    elif case == "another workload identity":
        token = mint(
            audience=run.audience, service_account="x@mcpforge-aa5c2.iam.gserviceaccount.com"
        )
        body = token
    elif case == "an expired token":
        token = mint(audience=run.audience, issued_ago=7200, expires_in=-3600)
        body = token
    elif case == "the debug image":
        token = mint(audience=run.audience, debug_status="enabled")
        body = token
    elif case == "no object yet":
        body = None
    elif case == "a JSON-wrapped object":
        body = json.dumps({"token": token})
    elif case == "an object with a trailing newline":
        body = token + "\n"
    elif case == "an empty object":
        body = b""
    elif case == "an oversized object":
        body = token + "." + "a" * MAX_TOKEN_BYTES
    elif case == "an unreadable bucket":
        bucket.failure = DeliveryUnreadableError("HTTP 403")
    elif case == "a run the API never issued":
        run_id = "cs-20260101-000000-abcdef"
    elif case == "a run issued too long ago":
        path = runs.directory / f"{run.run_id}.pending.json"
        record = json.loads(path.read_text())
        record["issued_at"] = (datetime.now(UTC) - RUN_TTL - timedelta(minutes=1)).isoformat()
        path.write_text(json.dumps(record))
    elif case == "a run already verified":
        assert runs.consume(run.run_id)
    if body is not None:
        bucket.put(run_id, body)
    return run_id, digest, token


@pytest.mark.parametrize("case", _refusal_cases())
async def test_every_refusal_leaves_the_api_and_panel_unattested(
    case: str,
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
    settings: Settings,
) -> None:
    bucket = FakeBucket()
    run_id, digest, _token = arrange(case, runs, bucket, make_attestation_token)
    executor = relying_party(runs, bucket, rsa_key, image_digest=digest)

    with build_client(settings, executor=executor) as client:
        _, session_id = make_session(client)
        response = client.post(f"/api/attestation-runs/{run_id}/verify", headers=auth())
        panel = trust_state(client, session_id)["secure_execution"]

    failure, attestation_failure = EXPECTED[case]
    body = response.json()
    assert body["verified"] is False
    assert body["failure"] == failure.value, body
    if attestation_failure is not None:
        assert body["attestation_failure"] == attestation_failure.value, body
    assert body["trust_level"] == "DEVELOPMENT_ISOLATION"
    assert panel["trust_level"] == "DEVELOPMENT_ISOLATION"
    assert panel["evidence"] is None
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION
    assert await executor.attestation() is None
    with pytest.raises(AttestationRequiredError):
        await executor.create_workspace(WorkspaceSpec(run_id="cs-job"))


async def test_a_missing_object_does_not_use_up_the_run(
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    """Asking before the VM has finished is not a replay; the run stays pending."""

    run = runs.issue()
    bucket = FakeBucket()
    executor = relying_party(runs, bucket, rsa_key)
    early = await executor.verify_run(run.run_id)
    assert early.failure is RunVerificationFailure.TOKEN_NOT_DELIVERED
    assert early.consumed is False

    bucket.put(run.run_id, make_attestation_token(audience=run.audience))
    assert (await executor.verify_run(run.run_id)).outcome.verified


async def test_a_rejected_token_uses_up_the_run(
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    run = runs.issue()
    bucket = FakeBucket()
    bucket.put(run.run_id, make_attestation_token(audience=run.audience, debug_status="enabled"))
    executor = relying_party(runs, bucket, rsa_key)
    assert (await executor.verify_run(run.run_id)).consumed is True
    record = runs.lookup(run.run_id)
    assert record is not None and record.consumed


def test_the_digest_the_api_verifies_against_is_its_own(
    runs: FileAttestationRunStore,
) -> None:
    """Nothing the operator or workload supplies carries a digest: the run record
    holds a run id, an audience and a time, and the policy's digest is a
    parameter of the relying party's configuration."""

    run = runs.issue()
    record = json.loads((runs.directory / f"{run.run_id}.pending.json").read_text())
    assert set(record) == {"run_id", "audience", "issued_at"}
    entrypoint = load_entrypoint()
    assert not any("DIGEST" in name for name in entrypoint.REQUIRED_ENVIRONMENT)


async def test_an_expired_verification_is_not_reused(
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    now = [datetime.now(UTC)]
    run = runs.issue()
    bucket = FakeBucket()
    bucket.put(run.run_id, make_attestation_token(audience=run.audience))
    executor = relying_party(runs, bucket, rsa_key, clock=lambda: now[0])
    result = await executor.verify_run(run.run_id)
    evidence = result.outcome.evidence
    assert evidence is not None

    now[0] = evidence.expires_at - timedelta(seconds=1)
    still_valid = executor.trust_level
    assert still_valid is TrustLevel.HARDWARE_ATTESTED
    now[0] = evidence.expires_at
    expired = executor.trust_level
    assert expired is TrustLevel.DEVELOPMENT_ISOLATION
    assert await executor.attestation() is None


async def test_nothing_is_attested_before_a_verification(
    runs: FileAttestationRunStore, rsa_key: rsa.RSAPrivateKey, tmp_path: Path
) -> None:
    executor = relying_party(runs, FakeBucket(), rsa_key)
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION
    assert await executor.attestation() is None
    workspace = Workspace(id="x", root=tmp_path, trust_level=TrustLevel.DEVELOPMENT_ISOLATION)
    with pytest.raises(AttestationRequiredError, match="no run has been verified"):
        await executor.run(workspace, Command(argv=("git", "--version")))


def test_the_executor_satisfies_the_port(
    runs: FileAttestationRunStore, rsa_key: rsa.RSAPrivateKey
) -> None:
    assert isinstance(relying_party(runs, FakeBucket(), rsa_key), SecureExecutionProvider)


def test_an_unusable_relying_party_configuration_is_refused(
    runs: FileAttestationRunStore,
) -> None:
    with pytest.raises(AttestationPolicyError):
        ConfidentialSpaceSecureExecutor(
            runs=runs, tokens=FakeBucket(), image_digest="latest", workload_service_account="x"
        )


def test_the_route_refuses_when_this_api_is_not_the_relying_party(settings: Settings) -> None:
    with build_client(settings) as client:
        response = client.post("/api/attestation-runs/cs-x/verify", headers=auth())
        assert response.status_code == 409
        assert client.post("/api/attestation-runs/cs-x/verify").status_code == 401


# ---------------------------------------------------------------------------
# 4. The relying party's reader and store
# ---------------------------------------------------------------------------


def _mock_reader(handler: Callable[[httpx.Request], httpx.Response]) -> GcsDeliveredTokenSource:
    return GcsDeliveredTokenSource(
        BUCKET,
        credentials=_Credentials("ya29.READER-CANARY"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_the_reader_asks_cloud_storage_for_exactly_the_run_object() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"a.b.c")

    assert _mock_reader(handler).fetch("attestation/cs-1.jwt") == b"a.b.c"
    (request,) = seen
    assert request.method == "GET"
    assert str(request.url) == (
        "https://storage.googleapis.com/storage/v1/b/mcpforge-aa5c2-attestation"
        "/o/attestation%2Fcs-1.jwt?alt=media"
    )
    assert request.headers["authorization"] == "Bearer ya29.READER-CANARY"


def _answer_with(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    return handler


def test_the_reader_reports_absence_as_none_and_anything_else_as_unreadable() -> None:
    assert _mock_reader(lambda _r: httpx.Response(404)).fetch("attestation/cs-1.jwt") is None
    for response in (
        httpx.Response(403),
        httpx.Response(200, content=b"a" * (MAX_TOKEN_BYTES + 1)),
    ):
        with pytest.raises(DeliveryUnreadableError) as caught:
            _mock_reader(_answer_with(response)).fetch("attestation/cs-1.jwt")
        assert "READER-CANARY" not in str(caught.value)


async def test_a_credential_failure_is_a_named_refusal_not_an_exception(
    runs: FileAttestationRunStore,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    """ADC that cannot refresh must not escape `verify_run` as an exception."""

    import google.auth.exceptions

    class _Expired:
        valid = False
        token: str | None = None

        def refresh(self, _request: object) -> None:
            raise google.auth.exceptions.RefreshError(  # type: ignore[no-untyped-call]
                "secret-detail-CANARY"
            )

    def unreachable(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request may be sent without a credential")

    reader = GcsDeliveredTokenSource(
        BUCKET,
        credentials=_Expired(),
        client=httpx.Client(transport=httpx.MockTransport(unreachable)),
    )
    run = runs.issue()
    executor = relying_party(runs, reader, rsa_key)
    result = await executor.verify_run(run.run_id)
    assert result.failure is RunVerificationFailure.DELIVERY_UNREADABLE
    assert "RefreshError" in (result.outcome.detail or "")
    assert "CANARY" not in (result.outcome.detail or "")
    assert result.consumed is False
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


@pytest.mark.parametrize(
    "content", ["", "not json", "[]", '{"run_id": "RUN"}', '{"run_id": "RUN", "audience": "a"}']
)
async def test_an_unreadable_run_record_is_an_unknown_run(
    content: str,
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    run = runs.issue()
    bucket = FakeBucket()
    bucket.put(run.run_id, make_attestation_token(audience=run.audience))
    (runs.directory / f"{run.run_id}.pending.json").write_text(content.replace("RUN", run.run_id))
    executor = relying_party(runs, bucket, rsa_key)
    result = await executor.verify_run(run.run_id)
    assert result.failure is RunVerificationFailure.UNKNOWN_RUN
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


def test_issued_runs_are_private_unique_and_single_use(runs: FileAttestationRunStore) -> None:
    issued = [runs.issue() for _ in range(20)]
    assert len({run.run_id for run in issued}) == 20
    assert len({run.audience for run in issued}) == 20
    for run in issued:
        check_launcher_audience(run.audience)
        assert len(run.audience) == len("mcpforge-attestation-") + 32
    assert stat.S_IMODE(os.stat(runs.directory).st_mode) == 0o700
    for path in runs.directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    first = issued[0].run_id
    assert runs.consume(first) is True
    assert runs.consume(first) is False
    record = runs.lookup(first)
    assert record is not None and record.consumed
    assert runs.lookup("cs-never-issued") is None
    assert runs.lookup("../escape") is None


def test_the_cli_issues_shows_and_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = str(tmp_path / "runs")
    assert relying_party_cli(["--store", store, "begin"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("=", 1)[0] for line in lines] == [
        "MCPFORGE_RUN_ID",
        "MCPFORGE_ATTESTATION_AUDIENCE",
    ]
    run_id = lines[0].split("=", 1)[1]

    assert relying_party_cli(["--store", store, "show", run_id]) == 0
    assert capsys.readouterr().out.splitlines() == lines
    assert relying_party_cli(["--store", store, "show", "cs-20260101-000000-abcdef"]) == 3
    assert FileAttestationRunStore(Path(store)).consume(run_id)
    assert relying_party_cli(["--store", store, "show", run_id]) == 3


# ---------------------------------------------------------------------------
# 5. The token never appears in any output, on either side
# ---------------------------------------------------------------------------

#: A token fragment of 16 base64url characters is one of 64**16 strings.
LEAK_WINDOW = 16


def leaked_fragments(token: str, haystack: str) -> list[str]:
    return sorted(
        {
            token[start : start + LEAK_WINDOW]
            for start in range(len(token) - LEAK_WINDOW + 1)
            if token[start : start + LEAK_WINDOW] in haystack
        }
    )


def test_the_leak_check_finds_a_leak() -> None:
    token = "eyJ" + "Q" * 60 + ".payload-part-" + "Z" * 40 + ".sig"
    assert leaked_fragments(token, f"log line: token={token[10:40]} end")
    assert leaked_fragments(token, "log line: nothing here") == []


async def test_the_token_never_appears_in_any_output(
    launcher: Launcher,
    socket_dir: Path,
    google: FakeGoogle,
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
    tmp_path: Path,
    settings: Settings,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workload side and API side, success and failure, with planted tokens and
    a planted access token, searched for in structlog events (before any
    redaction), stdlib logging, fd-level stdout/stderr, exception messages,
    `repr`s, the entrypoint's JSON and the API's responses."""

    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("HOME", str(tmp_path))
    jail = tmp_path / "jail"
    jail.mkdir()
    canary = {"planted": "LEAK-CANARY-" + "7" * 48}
    tokens: list[str] = []
    collected: list[str] = []
    entrypoint = load_entrypoint()

    def refuse_delivery(_token: confidential_space.AttestationToken, _run_id: str) -> str:
        raise DeliveryError(DeliveryFailure.UPLOAD_FAILED, "HTTP 500")

    executor = relying_party(runs, google.reader(), rsa_key)
    # `create_app` configures structlog; the capture must be installed after it,
    # or it is silently replaced and captures nothing.
    with (
        build_client(settings, executor=executor) as client,
        structlog.testing.capture_logs() as events,
    ):
        monkeypatch.setattr(
            confidential_space, "log", structlog.get_logger(confidential_space.__name__)
        )
        # Workload side: delivered, not delivered, and a launcher that sends the
        # token inside a malformed body.
        deliveries: list[tuple[str, Callable[[confidential_space.AttestationToken, str], str]]] = [
            ("token", google.deliver),
            ("token", refuse_delivery),
            ("newline", google.deliver),
        ]
        for served, deliver in deliveries:
            run = runs.issue()
            token = make_attestation_token(audience=run.audience, overrides=canary)
            tokens.append(token)
            path, _server = launcher(serve_token(token + ("\n" if served == "newline" else "")))
            _code, record = entrypoint.execute(
                {"MCPFORGE_RUN_ID": run.run_id, "MCPFORGE_ATTESTATION_AUDIENCE": run.audience},
                workspace_root=jail,
                socket_path=path,
                deliver=deliver,
            )
            collected.append(json.dumps(record, sort_keys=True))

        # API side: verified, rejected, replayed, and a malformed object that
        # holds the token.
        for case in ("verified", "debug", "replay", "newline"):
            run = runs.issue()
            token = make_attestation_token(
                audience=run.audience,
                overrides=canary,
                debug_status="enabled" if case == "debug" else "disabled-since-boot",
            )
            tokens.append(token)
            body = (token + "\n").encode() if case == "newline" else token.encode()
            google.put(run.run_id, body)
            for _ in range(2 if case == "replay" else 1):
                response = client.post(f"/api/attestation-runs/{run.run_id}/verify", headers=auth())
                collected.append(response.text)
            result = executor.last_verification
            collected += [repr(result), str(result.outcome.detail if result else "")]

        for text in (tokens[0] + "\n", json.dumps({"token": tokens[0]})):
            try:
                confidential_space.token_from_body(text.encode())
            except TokenRetrievalError as exc:
                collected += [str(exc), repr(exc)]

    streams = capfd.readouterr()
    haystack = "\n".join(
        [streams.out, streams.err, caplog.text, json.dumps(events, default=str), *collected]
    )

    received = [event for event in events if event.get("event") == "attestation.token_received"]
    assert received, f"no token_received event was captured: {events}"
    assert sha256_prefix(tokens[3]) in {event["token_sha256_prefix"] for event in received}
    assert sha256_prefix(tokens[3]) in haystack
    for planted in tokens:
        assert leaked_fragments(planted, haystack) == [], "a token fragment reached the output"
    assert leaked_fragments(google.access_token, haystack) == [], "the access token leaked"


def test_a_token_object_does_not_print_its_value(
    make_attestation_token: f8_01.MakeAttestationToken,
) -> None:
    token = make_attestation_token(audience=AUDIENCE)
    held = confidential_space.AttestationToken(token)
    for rendered in (repr(held), str(held), f"{held}", f"{held!r}"):
        assert leaked_fragments(token, rendered) == [], rendered
        assert sha256_prefix(token) in rendered


# ---------------------------------------------------------------------------
# 6. Google's signing keys, found by discovery
# ---------------------------------------------------------------------------


def test_the_discovery_document_is_googles_well_known_url() -> None:
    assert confidential_space.DISCOVERY_DOCUMENT_URL == (
        "https://confidentialcomputing.googleapis.com/.well-known/openid-configuration"
    )


class _KeyClient:
    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        class _Signing:
            key = self._key.public_key()

        return _Signing()


def _resolver(
    document: Any, rsa_key: rsa.RSAPrivateKey, built: list[str]
) -> ConfidentialSpaceKeyResolver:
    def factory(uri: str) -> Any:
        built.append(uri)
        return _KeyClient(rsa_key)

    return ConfidentialSpaceKeyResolver(
        fetch=lambda _url: json.dumps(document).encode(), jwks_client_factory=factory
    )


async def test_keys_are_resolved_from_the_discovered_jwks_uri(
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    run = runs.issue()
    bucket = FakeBucket()
    bucket.put(run.run_id, make_attestation_token(audience=run.audience))
    built: list[str] = []
    executor = ConfidentialSpaceSecureExecutor(
        runs=runs,
        tokens=bucket,
        image_digest=RUNNING_DIGEST,
        workload_service_account=WORKLOAD_SA,
        key_resolver=_resolver(
            {
                "issuer": "https://confidentialcomputing.googleapis.com",
                "jwks_uri": "https://keys.example.invalid/jwks",
            },
            rsa_key,
            built,
        ),
    )
    assert (await executor.verify_run(run.run_id)).outcome.verified
    assert built == ["https://keys.example.invalid/jwks"]


@pytest.mark.parametrize(
    "document",
    [
        {"issuer": "https://attacker.example", "jwks_uri": "https://keys.example.invalid/j"},
        {
            "issuer": "https://confidentialcomputing.googleapis.com",
            "jwks_uri": "http://keys.example.invalid/j",
        },
        {"issuer": "https://confidentialcomputing.googleapis.com"},
        ["https://keys.example.invalid/j"],
    ],
    ids=["another issuer", "plain http", "no jwks_uri", "not an object"],
)
async def test_an_untrustworthy_discovery_document_resolves_no_key(
    runs: FileAttestationRunStore,
    make_attestation_token: f8_01.MakeAttestationToken,
    rsa_key: rsa.RSAPrivateKey,
    document: Any,
) -> None:
    run = runs.issue()
    bucket = FakeBucket()
    bucket.put(run.run_id, make_attestation_token(audience=run.audience))
    executor = ConfidentialSpaceSecureExecutor(
        runs=runs,
        tokens=bucket,
        image_digest=RUNNING_DIGEST,
        workload_service_account=WORKLOAD_SA,
        key_resolver=_resolver(document, rsa_key, []),
    )
    result = await executor.verify_run(run.run_id)
    assert result.outcome.failure is AttestationFailure.UNRESOLVED_SIGNING_KEY
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


def test_discovery_refuses_a_non_https_url() -> None:
    resolver = ConfidentialSpaceKeyResolver(
        discovery_url="http://confidentialcomputing.googleapis.com/.well-known/openid-configuration"
    )
    with pytest.raises(ValueError, match="non-https"):
        resolver.discover_jwks_uri()


@pytest.mark.skipif(
    not os.environ.get("MCPFORGE_CONFIDENTIAL_SPACE_LIVE"),
    reason="live check against Google; set MCPFORGE_CONFIDENTIAL_SPACE_LIVE=1 to run",
)
def test_googles_discovery_document_names_a_usable_key_set() -> None:
    jwks_uri = ConfidentialSpaceKeyResolver().discover_jwks_uri()
    assert jwks_uri.startswith("https://")
    keys = jwt.PyJWKClient(jwks_uri, timeout=10).get_jwk_set().keys
    assert any(key.key_type == "RSA" for key in keys), keys


# ---------------------------------------------------------------------------
# 7. Structure
# ---------------------------------------------------------------------------

METADATA_SERVER_FRAGMENTS = ("metadata.google.internal", "169.254.169.254", "computeMetadata")
DELIVERY = SRC / "mcpforge" / "execution" / "token_delivery.py"
CLIENT = SRC / "mcpforge" / "execution" / "confidential_space.py"


def _string_constants(path: Path) -> list[str]:
    return [
        node.value
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_the_metadata_server_is_reached_only_by_the_delivery_transport() -> None:
    """The workload's storage credential comes from the metadata server, in one
    module; nothing else touches it, and nothing touches its identity endpoint —
    a VM identity token, which would be a counterfeit attestation."""

    constants = {path: _string_constants(path) for path in [*python_files(), ENTRYPOINT]}
    reaching = {
        path.name
        for path, values in constants.items()
        for value in values
        if any(fragment in value for fragment in METADATA_SERVER_FRAGMENTS)
    }
    assert reaching == {"token_delivery.py"}, reaching
    identity = [
        f"{path.name}: {value!r}"
        for path, values in constants.items()
        for value in values
        if "/identity" in value
    ]
    assert identity == []


def test_delivery_is_transport_and_attestation_is_not_transport() -> None:
    """`token_delivery` neither requests nor verifies a token; the launcher
    client does not import the delivery transport."""

    delivery_calls = {
        name
        for name in (
            "request_attestation_token",
            "verify_attestation_token",
            "verify_delivered_run",
        )
        for path, _fn, _line in call_sites(name)
        if path.endswith("token_delivery.py")
    }
    assert delivery_calls == set()
    assert not [m for _l, m in imported_modules(CLIENT) if m.endswith("token_delivery")]
    assert any(m.endswith("confidential_space") for _l, m in imported_modules(DELIVERY))


def test_production_code_cannot_reach_the_test_key() -> None:
    assert any(module.startswith("tests") for _line, module in imported_modules(Path(__file__)))
    assert files_importing(("tests", "conftest")) == []
    assert not [
        module
        for _line, module in imported_modules(ENTRYPOINT)
        if module.split(".")[0] in {"tests", "conftest"}
    ]


def test_the_attestation_modules_read_nothing_from_the_environment() -> None:
    """No variable can supply a token, a key, a digest or a policy value."""

    for path in (CLIENT, DELIVERY, SRC / "mcpforge" / "relying_party" / "runs.py"):
        tree = ast.parse(path.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        assert len(names) > 20, f"{path.name}: the sweep is reading nothing"
        assert not names & {"environ", "getenv", "environb"}, path.name


def test_the_run_id_rule_is_one_rule() -> None:
    assert load_entrypoint().RUN_ID_SHAPE.pattern == confidential_space.RUN_ID_SHAPE.pattern


def test_the_workload_bucket_is_the_bucket_setup_creates() -> None:
    setup = (ENTRYPOINT.parent / "setup.sh").read_text()
    assert f'readonly ATTESTATION_BUCKET="{load_entrypoint().ATTESTATION_BUCKET}"' in setup
    assert load_entrypoint().ATTESTATION_BUCKET == BUCKET


# ---------------------------------------------------------------------------
# 8. The entrypoint's refusals, in process
# ---------------------------------------------------------------------------


@pytest.fixture
def jail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / "jail"
    directory.mkdir()
    return directory


ENTRYPOINT_ENV = {"MCPFORGE_RUN_ID": "cs-test-0001", "MCPFORGE_ATTESTATION_AUDIENCE": AUDIENCE}


def test_the_entrypoint_refuses_when_no_token_is_obtained(
    socket_dir: Path, google: FakeGoogle, jail: Path
) -> None:
    code, record = load_entrypoint().execute(
        ENTRYPOINT_ENV,
        workspace_root=jail,
        socket_path=socket_dir / "absent.sock",
        deliver=google.deliver,
    )
    assert code == 16, record
    assert record["reason"] == "ATTESTATION_UNAVAILABLE"
    assert record["attestation"]["retrieval_failure"] == "SOCKET_UNAVAILABLE"
    assert record["preflight"]["workspace_root"] == str(jail)
    assert google.objects == {}


def test_the_entrypoint_refuses_when_the_token_is_not_delivered(
    launcher: Launcher,
    google: FakeGoogle,
    make_attestation_token: f8_01.MakeAttestationToken,
    jail: Path,
) -> None:
    path, _server = launcher(serve_token(make_attestation_token(audience=AUDIENCE)))
    google.upload_status = 403
    code, record = load_entrypoint().execute(
        ENTRYPOINT_ENV, workspace_root=jail, socket_path=path, deliver=google.deliver
    )
    assert code == 17, record
    assert record["reason"] == "DELIVERY_FAILED"
    assert record["attestation"]["token_obtained"] is True
    assert record["attestation"]["delivery_failure"] == "UPLOAD_REFUSED"
