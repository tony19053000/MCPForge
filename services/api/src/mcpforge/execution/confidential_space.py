"""Confidential Space attestation — F8-02. Both halves of the path, and the executor.

**The trust model.** A Confidential Space attestation token is signed by Google.
It is therefore *self-authenticating*: whoever holds it can check it, and the
channel that carried it does not need to be trusted. What matters is **who
checks it**. A workload that checks its own token proves nothing to anyone —
a malicious image would simply report success — so the workload does not check
it at all. It obtains the token and hands it over; the MCPForge API service,
outside the TEE, is the relying party and is the only party whose verification
counts.

The path, end to end:

1. **The relying party issues a run** (`mcpforge.relying_party`): a run id and
   a fresh audience — the one-time nonce — recorded as pending. Replay
   protection means something only because the verifier chose the nonce.
2. **`launch.sh` boots the VM with that run's values**, taken from the record;
   it has no way to accept an audience of its own.
3. **The workload requests a token** from the Confidential Space launcher with
   that audience (`request_attestation_token`, below): `POST /v1/token` over
   `/run/container_launcher/teeserver.sock`, body
   `{"audience": ..., "token_type": "OIDC"}`, the raw JWT back — per Google's
   documentation and the launcher's handler (`launcher/teeserver/tee_server.go`
   in `google/go-tpm-tools`, which decodes with `DisallowUnknownFields`).
4. **The workload writes the raw token** to the private object
   `gs://<bucket>/attestation/<run_id>.jwt` (`token_delivery.py`). That is
   transport, not logging; the token still appears in no log, stdout or
   exception anywhere.
5. **The API fetches the object and verifies it** (`verify_delivered_run`,
   below) with F8-01's `verify_attestation_token`, against the audience **it
   issued for that run**, the image digest **from its own configuration**, the
   workload service account, and Google's keys from the issuer's discovery
   document. Each run is verified once; a second attempt is refused.
6. **Only that verification raises the executor's trust level**, and only then
   can the trust panel show it, through F8-03's single existing branch.

**What this module never does**, each pinned by a named test:

- Name the attested trust level, or construct `AttestationEvidence` — F8-01's
  `test_exactly_one_function_can_produce_the_attested_trust_level` and
  `test_evidence_is_constructed_in_exactly_one_place` pass unchanged.
- Obtain a token any way but the launcher, or verify one anywhere but
  `verify_delivered_run` — `test_the_only_attestation_path_is_the_relying_party`.
- Touch the Compute metadata server. The workload's *storage* credential comes
  from there, in `token_delivery.py` and nowhere else; the metadata identity
  endpoint is not an attestation and is used by nothing —
  `test_the_metadata_server_is_reached_only_by_the_delivery_transport`.
- Log, print, raise or `repr` a token — `test_the_token_never_appears_in_any_output`.

**What cannot be proven in this repository.** The tests run the real launcher
client against a local Unix-socket server implementing the documented contract,
the real delivery code against local stand-ins for the metadata server and
Cloud Storage, and mint tokens only with F8-01's test key. That exercises every
piece of code on the path; it is not attestation. `F8-02` completes only when a
real Confidential Space run produces a token that the API verifies against its
own pinned digest and issued nonce, and until then it is `BLOCKED`.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import re
import socket
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import anyio.to_thread
import jwt

from mcpforge.execution.attestation import (
    CONFIDENTIAL_SPACE_ISSUER,
    AttestationEvidence,
    AttestationFailure,
    AttestationKeyResolver,
    AttestationOutcome,
    AttestationPolicy,
    JwksAttestationKeyResolver,
    TrustLevel,
    verify_attestation_token,
)
from mcpforge.execution.provider import (
    Command,
    CommandResult,
    SandboxError,
    Workspace,
    WorkspaceSpec,
)
from mcpforge.logging import get_logger

log = get_logger(__name__)

# -- the launcher's documented interface -------------------------------------

#: Where the Confidential Space launcher listens inside the workload container.
TEE_SERVER_SOCKET = Path("/run/container_launcher/teeserver.sock")
#: Google Cloud Attestation. (`/v1/intel/token` is Intel Trust Authority, which
#: MCPForge does not use.)
# S105 reads "TOKEN" as a credential name; these are protocol constants.
TOKEN_ENDPOINT_PATH = "/v1/token"  # noqa: S105
#: `OIDC` tokens are validated against the rotating keys named by `jwks_uri`.
TOKEN_TYPE = "OIDC"  # noqa: S105
#: Google: "The maximum length is 512 bytes." Bytes, not characters.
MAX_AUDIENCE_BYTES = 512
#: Google: "The value https://sts.google.com can't be used when setting a
#: custom audience."
RESERVED_AUDIENCE = "https://sts.google.com"

#: A Confidential Space token is a few kilobytes. 64 KiB is far above any real
#: token and far below anything that could hurt a reader. The same cap applies
#: to the launcher's response and to the object the API reads back.
MAX_TOKEN_BYTES = 64 * 1024
#: Hard deadline for the whole launcher exchange, enforced by a watchdog so a
#: server that drips one byte at a time cannot stretch it.
DEFAULT_TOKEN_TIMEOUT_SECONDS = 30.0
DEFAULT_SOCKET_OPERATION_TIMEOUT_SECONDS = 10.0

#: Exactly one compact JWS: three non-empty base64url segments, nothing else.
_COMPACT_JWS = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

#: A run id becomes an object name and a filename, so its shape is fixed. The
#: workload entrypoint holds a copy of this pattern (it must refuse before the
#: payload is imported); `test_the_run_id_rule_is_one_rule` fails if they differ.
RUN_ID_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Where the workload writes a run's token inside the attestation bucket.
RUN_OBJECT_PREFIX = "attestation/"

# -- Google's OIDC discovery document ----------------------------------------

#: Google: the validation endpoint "is constructed by appending
#: .well-known/openid-configuration to the issuer URI". Fetched on 2026-09-10,
#: it named `https://www.googleapis.com/service_accounts/v1/metadata/jwk/signer@confidentialspace-sign.iam.gserviceaccount.com`
#: as `jwks_uri`; that is an observation, not a constant used anywhere.
DISCOVERY_DOCUMENT_URL = CONFIDENTIAL_SPACE_ISSUER + "/.well-known/openid-configuration"
MAX_DISCOVERY_BYTES = 64 * 1024
DISCOVERY_TIMEOUT_SECONDS = 10.0
JWKS_TIMEOUT_SECONDS = 10.0


class TokenRetrievalFailure(StrEnum):
    """Why no token was obtained. Every one of these means no token."""

    SOCKET_UNAVAILABLE = "SOCKET_UNAVAILABLE"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    TIMEOUT = "TIMEOUT"
    HTTP_STATUS = "HTTP_STATUS"
    OVERSIZED_RESPONSE = "OVERSIZED_RESPONSE"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    INVALID_AUDIENCE = "INVALID_AUDIENCE"


class TokenRetrievalError(Exception):
    """No token was obtained. The message never contains a response body."""

    def __init__(self, failure: TokenRetrievalFailure, detail: str) -> None:
        super().__init__(f"{failure.value}: {detail}")
        self.failure = failure
        self.detail = detail


class InvalidAudienceError(ValueError):
    """The audience breaks one of Google's documented constraints."""


def check_launcher_audience(audience: str) -> None:
    """Google's constraints on a custom audience. Raises `InvalidAudienceError`."""
    if not isinstance(audience, str) or not audience:
        raise InvalidAudienceError("the audience is empty")
    size = len(audience.encode("utf-8"))
    if size > MAX_AUDIENCE_BYTES:
        raise InvalidAudienceError(
            f"the audience is {size} bytes; the launcher accepts at most {MAX_AUDIENCE_BYTES}"
        )
    if audience == RESERVED_AUDIENCE:
        raise InvalidAudienceError(f"{RESERVED_AUDIENCE} cannot be used as a custom audience")


def run_object_name(run_id: str) -> str:
    """`attestation/<run_id>.jwt`, for a run id of the permitted shape only."""
    if not RUN_ID_SHAPE.fullmatch(run_id):
        raise ValueError("the run id does not match the permitted shape")
    return f"{RUN_OBJECT_PREFIX}{run_id}.jwt"


@dataclass(frozen=True)
class TokenFingerprint:
    """What may be recorded about a token: a non-reversible prefix and a length."""

    sha256_prefix: str
    length: int


def fingerprint_of(token: str) -> TokenFingerprint:
    return TokenFingerprint(
        sha256_prefix=hashlib.sha256(token.encode("utf-8")).hexdigest()[:16],
        length=len(token),
    )


@dataclass(frozen=True, repr=False)
class AttestationToken:
    """A raw token. `repr` and `str` show the fingerprint, never the value."""

    value: str

    @property
    def fingerprint(self) -> TokenFingerprint:
        return fingerprint_of(self.value)

    def __repr__(self) -> str:
        fingerprint = self.fingerprint
        return (
            f"AttestationToken(sha256_prefix={fingerprint.sha256_prefix!r}, "
            f"length={fingerprint.length})"
        )

    __str__ = __repr__


def token_from_body(payload: bytes) -> AttestationToken:
    """The one rule for "this is a token": exactly one compact JWS, capped.

    Used for the launcher's response *and* for the object the API reads back,
    so the two ends cannot disagree about what a token looks like. Raises
    `TokenRetrievalError`; the message never contains the body.
    """
    if not payload:
        raise TokenRetrievalError(TokenRetrievalFailure.EMPTY_RESPONSE, "the body is empty")
    if len(payload) > MAX_TOKEN_BYTES:
        raise TokenRetrievalError(
            TokenRetrievalFailure.OVERSIZED_RESPONSE,
            f"{len(payload)} bytes; the cap is {MAX_TOKEN_BYTES}",
        )
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TokenRetrievalError(
            TokenRetrievalFailure.MALFORMED_RESPONSE,
            f"the {len(payload)}-byte body is not ASCII",
        ) from exc
    if not _COMPACT_JWS.fullmatch(text):
        raise TokenRetrievalError(
            TokenRetrievalFailure.MALFORMED_RESPONSE,
            f"the {len(payload)}-byte body is not exactly one compact JWS",
        )
    return AttestationToken(text)


class _UnixSocketHTTPConnection(http.client.HTTPConnection):
    """`http.client` over `AF_UNIX`. The `Host` header stays `localhost`."""

    def __init__(self, socket_path: Path, operation_timeout: float) -> None:
        super().__init__("localhost", timeout=operation_timeout)
        self._socket_path = socket_path
        #: Kept so the watchdog can abort a blocked read from another thread.
        self.raw_socket: socket.socket | None = None

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        self.raw_socket = sock
        try:
            sock.connect(str(self._socket_path))
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _exchange(connection: _UnixSocketHTTPConnection, body: bytes) -> tuple[int, bytes]:
    """Send the request; return the status and, for a 200 only, the capped body."""
    connection.request(
        "POST",
        TOKEN_ENDPOINT_PATH,
        body=body,
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    if response.status != http.HTTPStatus.OK:
        # The body of an error is not read: it is not needed to fail closed.
        return response.status, b""

    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except ValueError as exc:
            raise TokenRetrievalError(
                TokenRetrievalFailure.MALFORMED_RESPONSE, "unparseable Content-Length"
            ) from exc
        if declared_length > MAX_TOKEN_BYTES:
            raise TokenRetrievalError(
                TokenRetrievalFailure.OVERSIZED_RESPONSE,
                f"declared {declared_length} bytes; the cap is {MAX_TOKEN_BYTES}",
            )

    received = bytearray()
    while True:
        # At most one read on the socket per call, so the watchdog can interrupt.
        chunk = response.read1(4096)
        if not chunk:
            break
        received += chunk
        if len(received) > MAX_TOKEN_BYTES:
            raise TokenRetrievalError(
                TokenRetrievalFailure.OVERSIZED_RESPONSE,
                f"more than {MAX_TOKEN_BYTES} bytes received",
            )
    return response.status, bytes(received)


def _classify(exc: BaseException, *, expired: bool) -> TokenRetrievalError:
    """Map a transport exception to a named failure. The deadline wins every tie."""
    if expired:
        return TokenRetrievalError(
            TokenRetrievalFailure.TIMEOUT, "the token exchange exceeded its deadline"
        )
    if isinstance(exc, TokenRetrievalError):
        return exc
    if isinstance(exc, FileNotFoundError):
        return TokenRetrievalError(
            TokenRetrievalFailure.SOCKET_UNAVAILABLE,
            "the launcher socket does not exist; this process is not running under "
            "the Confidential Space launcher",
        )
    if isinstance(exc, TimeoutError):
        return TokenRetrievalError(
            TokenRetrievalFailure.TIMEOUT, "a socket operation exceeded its timeout"
        )
    if isinstance(exc, http.client.RemoteDisconnected):
        return TokenRetrievalError(
            TokenRetrievalFailure.CONNECTION_FAILED, "the launcher closed the connection"
        )
    if isinstance(exc, http.client.HTTPException):
        return TokenRetrievalError(
            TokenRetrievalFailure.MALFORMED_RESPONSE,
            f"the response is not valid HTTP: {type(exc).__name__}",
        )
    if isinstance(exc, OSError):
        return TokenRetrievalError(
            TokenRetrievalFailure.CONNECTION_FAILED,
            f"could not talk to the launcher: {type(exc).__name__}: {exc.strerror}",
        )
    return TokenRetrievalError(
        TokenRetrievalFailure.CONNECTION_FAILED,
        f"unexpected failure talking to the launcher: {type(exc).__name__}",
    )


def request_attestation_token(
    audience: str,
    *,
    socket_path: Path = TEE_SERVER_SOCKET,
    timeout_seconds: float = DEFAULT_TOKEN_TIMEOUT_SECONDS,
    operation_timeout_seconds: float = DEFAULT_SOCKET_OPERATION_TIMEOUT_SECONDS,
) -> AttestationToken:
    """Ask the Confidential Space launcher for a token. Workload side only.

    Returns a token or raises `TokenRetrievalError`; there is no fallback
    source. The token is *not* verified here — a workload vouching for itself
    proves nothing — it is delivered to the relying party.
    """
    try:
        check_launcher_audience(audience)
    except InvalidAudienceError as exc:
        raise TokenRetrievalError(TokenRetrievalFailure.INVALID_AUDIENCE, str(exc)) from exc
    if timeout_seconds <= 0 or operation_timeout_seconds <= 0:
        raise ValueError("token retrieval timeouts must be positive")

    body = json.dumps({"audience": audience, "token_type": TOKEN_TYPE}).encode("utf-8")
    connection = _UnixSocketHTTPConnection(
        socket_path, min(operation_timeout_seconds, timeout_seconds)
    )
    expired = threading.Event()

    def abort() -> None:
        expired.set()
        raw = connection.raw_socket
        if raw is not None:
            with contextlib.suppress(OSError):
                raw.shutdown(socket.SHUT_RDWR)

    watchdog = threading.Timer(timeout_seconds, abort)
    watchdog.daemon = True
    watchdog.start()
    try:
        status, payload = _exchange(connection, body)
    except Exception as exc:
        raise _classify(exc, expired=expired.is_set()) from exc
    finally:
        watchdog.cancel()
        connection.close()
    if expired.is_set():
        raise _classify(TimeoutError(), expired=True)
    if status != http.HTTPStatus.OK:
        raise TokenRetrievalError(
            TokenRetrievalFailure.HTTP_STATUS,
            f"the launcher answered HTTP {status}; the response body is not recorded",
        )
    return token_from_body(payload)


# -- Google's signing keys ----------------------------------------------------

FetchDocument = Callable[[str], bytes]
JwksClientFactory = Callable[[str], Any]


def _https_get(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"refusing to fetch a non-https URL: {url!r}")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=DISCOVERY_TIMEOUT_SECONDS) as response:  # noqa: S310
        data: bytes = response.read(MAX_DISCOVERY_BYTES + 1)
    if len(data) > MAX_DISCOVERY_BYTES:
        raise ValueError(f"{url} returned more than {MAX_DISCOVERY_BYTES} bytes")
    return data


def _default_jwks_client(jwks_uri: str) -> Any:
    return jwt.PyJWKClient(jwks_uri, cache_keys=True, timeout=JWKS_TIMEOUT_SECONDS)


class ConfidentialSpaceKeyResolver:
    """Resolves Google's Confidential Space signing keys via OIDC discovery.

    The discovery document is refused unless its `issuer` is exactly the issuer
    F8-01 checks and its `jwks_uri` is https. Any failure raises, which
    `verify_attestation_token` turns into `UNRESOLVED_SIGNING_KEY`.

    `fetch` and `jwks_client_factory` replace network I/O only; whatever they
    return, the signature is still verified against it.
    """

    def __init__(
        self,
        *,
        discovery_url: str = DISCOVERY_DOCUMENT_URL,
        expected_issuer: str = CONFIDENTIAL_SPACE_ISSUER,
        fetch: FetchDocument = _https_get,
        jwks_client_factory: JwksClientFactory = _default_jwks_client,
    ) -> None:
        self._discovery_url = discovery_url
        self._expected_issuer = expected_issuer
        self._fetch = fetch
        self._jwks_client_factory = jwks_client_factory
        self._delegate: JwksAttestationKeyResolver | None = None

    def discover_jwks_uri(self) -> str:
        document = json.loads(self._fetch(self._discovery_url))
        if not isinstance(document, dict):
            raise ValueError("the discovery document is not a JSON object")
        issuer = document.get("issuer")
        if issuer != self._expected_issuer:
            raise ValueError(
                f"the discovery document names issuer {issuer!r}, not {self._expected_issuer!r}"
            )
        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri.startswith("https://"):
            raise ValueError(f"the discovery document's jwks_uri is not https: {jwks_uri!r}")
        return jwks_uri

    def signing_key_for(self, token: str) -> Any:
        if self._delegate is None:
            jwks_uri = self.discover_jwks_uri()
            self._delegate = JwksAttestationKeyResolver(
                jwks_uri, client=self._jwks_client_factory(jwks_uri)
            )
        return self._delegate.signing_key_for(token)


# -- the relying party --------------------------------------------------------

#: How long an issued run may wait for its token. A pending run older than this
#: is refused by `launch.sh` and by verification; the token's own `exp` (one
#: hour) is checked separately by F8-01.
RUN_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class AttestationRun:
    """A run the relying party issued. The audience is the nonce it chose."""

    run_id: str
    audience: str
    issued_at: datetime


@dataclass(frozen=True)
class RunRecord:
    run: AttestationRun
    consumed: bool


@runtime_checkable
class AttestationRunStore(Protocol):
    """Where issued runs live. `consume` is atomic and succeeds exactly once."""

    def lookup(self, run_id: str) -> RunRecord | None: ...

    def consume(self, run_id: str) -> bool: ...


class DeliveryUnreadableError(Exception):
    """The token object could not be read for a reason other than absence."""


@runtime_checkable
class DeliveredTokenSource(Protocol):
    """Reads a delivered token object. `None` means the object does not exist."""

    def fetch(self, object_name: str) -> bytes | None: ...


class RunVerificationFailure(StrEnum):
    UNKNOWN_RUN = "UNKNOWN_RUN"
    RUN_ALREADY_CONSUMED = "RUN_ALREADY_CONSUMED"
    RUN_EXPIRED = "RUN_EXPIRED"
    TOKEN_NOT_DELIVERED = "TOKEN_NOT_DELIVERED"  # noqa: S105
    DELIVERY_UNREADABLE = "DELIVERY_UNREADABLE"
    OBJECT_MALFORMED = "OBJECT_MALFORMED"
    TOKEN_REJECTED = "TOKEN_REJECTED"  # noqa: S105


@dataclass(frozen=True)
class RunVerification:
    """The result of verifying one run. Holds a fingerprint, never a token."""

    run_id: str
    outcome: AttestationOutcome
    failure: RunVerificationFailure | None
    token: TokenFingerprint | None
    #: Whether this attempt used up the run. Absence and an unreadable object
    #: do not; anything that read a token does, whatever the verdict.
    consumed: bool

    def __post_init__(self) -> None:
        if self.outcome.verified and self.failure is not None:
            raise ValueError("a verified run cannot also record a failure")
        if not self.outcome.verified and self.failure is None:
            raise ValueError("an unverified run must record why")


def _refused(
    run_id: str,
    failure: RunVerificationFailure,
    detail: str,
    *,
    token: TokenFingerprint | None = None,
    consumed: bool = False,
) -> RunVerification:
    log.warning("attestation.run_refused", run_id=run_id, failure=failure.value)
    return RunVerification(
        run_id=run_id,
        outcome=AttestationOutcome.rejected(AttestationFailure.NO_TOKEN, detail),
        failure=failure,
        token=token,
        consumed=consumed,
    )


def verify_delivered_run(
    run_id: str,
    *,
    runs: AttestationRunStore,
    tokens: DeliveredTokenSource,
    image_digest: str,
    workload_service_account: str,
    key_resolver: AttestationKeyResolver,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    run_ttl: timedelta = RUN_TTL,
) -> RunVerification:
    """The relying party's check of one run's delivered token.

    In order: the run was issued by this relying party; it has not been
    consumed; it has not outlived `run_ttl`; its object exists and is exactly
    one compact JWS; the run is consumed — atomically, so a concurrent second
    verifier loses; and the token passes `verify_attestation_token` against
    the audience issued for this run, `image_digest` from the relying party's
    own configuration, and `workload_service_account`. No value here comes
    from the operator or the workload. Only the last step can raise the trust
    level, and only through F8-01.
    """
    record = runs.lookup(run_id)
    if record is None:
        return _refused(
            run_id, RunVerificationFailure.UNKNOWN_RUN, "this relying party issued no such run"
        )
    if record.consumed:
        return _refused(
            run_id,
            RunVerificationFailure.RUN_ALREADY_CONSUMED,
            "this run's token has already been verified once; a run is single-use",
        )
    if clock() - record.run.issued_at > run_ttl:
        return _refused(
            run_id, RunVerificationFailure.RUN_EXPIRED, "the run was issued too long ago"
        )

    try:
        body = tokens.fetch(run_object_name(run_id))
    except DeliveryUnreadableError as exc:
        return _refused(
            run_id, RunVerificationFailure.DELIVERY_UNREADABLE, f"could not read the object: {exc}"
        )
    if body is None:
        return _refused(
            run_id,
            RunVerificationFailure.TOKEN_NOT_DELIVERED,
            "no token object exists for this run yet",
        )

    # From here the run is used up whatever the verdict: a delivered token is
    # judged exactly once. `consume` is the atomic step; losing it means
    # another verifier got there first, and that is a replay.
    if not runs.consume(run_id):
        return _refused(
            run_id,
            RunVerificationFailure.RUN_ALREADY_CONSUMED,
            "another verification consumed this run first",
        )

    try:
        token = token_from_body(body)
    except TokenRetrievalError as exc:
        return _refused(
            run_id,
            RunVerificationFailure.OBJECT_MALFORMED,
            f"the token object is not a token: {exc.failure.value}",
            consumed=True,
        )

    fingerprint = token.fingerprint
    log.info(
        "attestation.token_received",
        run_id=run_id,
        token_sha256_prefix=fingerprint.sha256_prefix,
        token_length=fingerprint.length,
    )
    policy = AttestationPolicy(
        audience=record.run.audience,
        image_digest=image_digest,
        workload_service_account=workload_service_account,
    )
    outcome = verify_attestation_token(
        token.value, policy=policy, key_resolver=key_resolver, clock=clock
    )
    log.info(
        "attestation.token_checked",
        run_id=run_id,
        verified=outcome.verified,
        failure=outcome.failure.value if outcome.failure is not None else None,
    )
    return RunVerification(
        run_id=run_id,
        outcome=outcome,
        failure=None if outcome.verified else RunVerificationFailure.TOKEN_REJECTED,
        token=fingerprint,
        consumed=True,
    )


# -- the executor ------------------------------------------------------------


class AttestationRequiredError(SandboxError):
    """A job was refused because no in-date verified attestation exists."""


class NoAttestedJobRunnerError(SandboxError):
    """A job was refused because the attested workload has no job runner."""


class ConfidentialSpaceSecureExecutor:
    """`SecureExecutionProvider` for the relying party. Lives in the API service.

    Its trust level is `HARDWARE_ATTESTED` only after `verify_run` has verified
    a delivered token through `verify_delivered_run`, and only while that
    token's `exp` holds; each new attempt replaces the last, so a failed one
    drops the level. `attestation()` returns F8-01's evidence or `None`.

    **It runs no job, attested or not.** The attested workload has no job
    runner yet, and running a job here, on the API host, under an attested
    label would claim a boundary the job was never inside. So `create_workspace`
    and `run` refuse in every state: `AttestationRequiredError` without verified
    evidence, `NoAttestedJobRunnerError` with it.
    """

    def __init__(
        self,
        *,
        runs: AttestationRunStore,
        tokens: DeliveredTokenSource,
        image_digest: str,
        workload_service_account: str,
        key_resolver: AttestationKeyResolver | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        run_ttl: timedelta = RUN_TTL,
    ) -> None:
        # Validates the configured digest and account up front: an unusable
        # relying-party configuration fails at construction, not at a VM run.
        AttestationPolicy(
            audience="configuration-check",
            image_digest=image_digest,
            workload_service_account=workload_service_account,
        )
        self._runs = runs
        self._tokens = tokens
        self._image_digest = image_digest
        self._workload_service_account = workload_service_account
        self._key_resolver: AttestationKeyResolver = (
            key_resolver if key_resolver is not None else ConfidentialSpaceKeyResolver()
        )
        self._clock = clock
        self._run_ttl = run_ttl
        self._last: RunVerification | None = None

    @property
    def last_verification(self) -> RunVerification | None:
        return self._last

    def verify_run_blocking(self, run_id: str) -> RunVerification:
        result = verify_delivered_run(
            run_id,
            runs=self._runs,
            tokens=self._tokens,
            image_digest=self._image_digest,
            workload_service_account=self._workload_service_account,
            key_resolver=self._key_resolver,
            clock=self._clock,
            run_ttl=self._run_ttl,
        )
        self._last = result
        return result

    async def verify_run(self, run_id: str) -> RunVerification:
        return await anyio.to_thread.run_sync(self.verify_run_blocking, run_id)

    def _current_evidence(self) -> AttestationEvidence | None:
        last = self._last
        if last is None:
            return None
        evidence = last.outcome.evidence
        if evidence is None:
            return None
        if self._clock() >= evidence.expires_at:
            return None
        return evidence

    @property
    def trust_level(self) -> TrustLevel:
        # Never names the attested member: it is the verified outcome's level,
        # or development isolation.
        last = self._last
        if last is None or self._current_evidence() is None:
            return TrustLevel.DEVELOPMENT_ISOLATION
        return last.outcome.trust_level

    async def attestation(self) -> AttestationEvidence | None:
        """The relying party's verified evidence, or `None`. Never stale."""
        return self._current_evidence()

    def _refuse_job(self) -> None:
        if self._current_evidence() is None:
            last = self._last
            why = (
                "no run has been verified"
                if last is None
                else "the last verification failed"
                if last.outcome.evidence is None
                else "the verified attestation has expired"
            )
            raise AttestationRequiredError(
                f"Refusing to run a job without verified hardware attestation: {why}. "
                "There is no fallback to development isolation."
            )
        raise NoAttestedJobRunnerError(
            "The attested workload has no job runner. Refusing to run a job on the API "
            "host under an attested label."
        )

    async def create_workspace(self, spec: WorkspaceSpec) -> Workspace:
        del spec
        self._refuse_job()
        raise AssertionError("unreachable")  # pragma: no cover

    async def run(self, workspace: Workspace, command: Command) -> CommandResult:
        del workspace, command
        self._refuse_job()
        raise AssertionError("unreachable")  # pragma: no cover

    async def destroy(self, workspace: Workspace) -> None:
        del workspace
        raise SandboxError("this executor creates no workspaces")
