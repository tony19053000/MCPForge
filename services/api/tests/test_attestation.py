"""Attestation evidence and verification — F8-01. The T7 control.

Every token in this module is a real RS256 JWT signed by a locally generated key
pair, so signature, algorithm, issuer, audience and expiry are genuinely checked
rather than mocked away. A real Confidential Space token is not available here
(blocker B-04) and is not simulated: what these tests prove is that *our*
verification rejects everything it should, and that nothing in the backend can
claim attestation without passing through it.
"""

from __future__ import annotations

import ast
import base64
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mcpforge.execution.attestation import (
    ACCEPTED_ALGORITHMS,
    CONFIDENTIAL_SPACE_ISSUER,
    AttestationEvidence,
    AttestationFailure,
    AttestationKeyResolver,
    AttestationOutcome,
    AttestationPolicy,
    AttestationPolicyError,
    AttestationVerifier,
    ConfidentialSpaceAttestationVerifier,
    JwksAttestationKeyResolver,
    TrustLevel,
    verify_attestation_token,
)
from tests.structure import SRC, python_files

AUDIENCE = "mcpforge-run-b3f1c0"
IMAGE_DIGEST = "sha256:" + "ab" * 32
OTHER_DIGEST = "sha256:" + "cd" * 32
WORKLOAD_SA = "mcpforge-workload@launchforge-tee.iam.gserviceaccount.com"


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def policy() -> AttestationPolicy:
    return AttestationPolicy(
        audience=AUDIENCE,
        image_digest=IMAGE_DIGEST,
        workload_service_account=WORKLOAD_SA,
    )


@pytest.fixture(scope="session")
def attacker_key() -> rsa.RSAPrivateKey:
    """A well-formed key that is simply not the one we trust."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class _Resolver:
    """Serves one public key, standing in for a JWKS endpoint.

    Only the key *lookup* is substituted. Signature verification itself runs for
    real against this key, so a token signed by anything else fails.
    """

    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        self.calls = 0
        self._public = key.public_key()

    def signing_key_for(self, token: str) -> Any:
        self.calls += 1
        return self._public


@pytest.fixture
def resolver(rsa_key: rsa.RSAPrivateKey) -> _Resolver:
    return _Resolver(rsa_key)


MakeAttestationToken = Callable[..., str]


@pytest.fixture
def make_attestation_token(private_pem: str) -> MakeAttestationToken:
    """Mints a token shaped like a Confidential Space attestation token."""

    def _make(
        *,
        key: str | None = None,
        algorithm: str = "RS256",
        audience: str = AUDIENCE,
        issuer: str = CONFIDENTIAL_SPACE_ISSUER,
        expires_in: int = 3600,
        issued_ago: int = 0,
        not_before_in: int | None = None,
        image_digest: str = IMAGE_DIGEST,
        image_reference: str = "europe-docker.pkg.dev/launchforge-tee/mcpforge/worker:1.4.0",
        service_account: str = WORKLOAD_SA,
        hardware_model: str = "GCP_AMD_SEV",
        software_name: str = "CONFIDENTIAL_SPACE",
        debug_status: str = "disabled-since-boot",
        overrides: dict[str, Any] | None = None,
        omit: tuple[str, ...] = (),
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer,
            "aud": audience,
            "sub": "https://www.googleapis.com/compute/v1/projects/launchforge-tee/zones/"
            "europe-west4-a/instances/mcpforge-worker-1",
            "iat": now - issued_ago,
            "exp": now + expires_in,
            "google_service_account": service_account,
            "hwmodel": hardware_model,
            "swname": software_name,
            "dbgstat": debug_status,
            "submods": {
                "container": {
                    "image_digest": image_digest,
                    "image_reference": image_reference,
                }
            },
        }
        if not_before_in is not None:
            claims["nbf"] = now + not_before_in
        if overrides:
            claims.update(overrides)
        for name in omit:
            claims.pop(name, None)
        return jwt.encode(
            claims,
            key if key is not None else private_pem,
            algorithm=algorithm,
            headers={"kid": "attestation-key-1"},
        )

    return _make


SignClaims = Callable[[dict[str, Any]], str]


@pytest.fixture
def sign_claims(private_pem: str) -> SignClaims:
    """Signs an arbitrary claim set, bypassing PyJWT's own encode-side checks.

    `jwt.encode` refuses to mint some of the shapes this module has to survive —
    `iss: 17` raises `TypeError` in the encoder, for instance — but a hostile or
    simply broken issuer is under no such constraint. Signing the payload through
    the JWS layer produces a genuinely signed token carrying whatever claims we
    ask for, which is the only way to test what happens when one arrives.
    """

    def _sign(claims: dict[str, Any]) -> str:
        return jwt.api_jws.encode(
            json.dumps(claims).encode(),
            private_pem,
            algorithm="RS256",
            headers={"kid": "attestation-key-1"},
        )

    return _sign


@pytest.fixture
def base_claims() -> dict[str, Any]:
    """The claim set of a token that verifies, as a plain dict."""
    now = int(time.time())
    return {
        "iss": CONFIDENTIAL_SPACE_ISSUER,
        "aud": AUDIENCE,
        "sub": "//compute.googleapis.com/instances/mcpforge-worker-1",
        "iat": now,
        "exp": now + 3600,
        "google_service_account": WORKLOAD_SA,
        "hwmodel": "GCP_AMD_SEV",
        "swname": "CONFIDENTIAL_SPACE",
        "dbgstat": "disabled-since-boot",
        "submods": {
            "container": {
                "image_digest": IMAGE_DIGEST,
                "image_reference": "europe-docker.pkg.dev/p/mcpforge/worker:1.4.0",
            }
        },
    }


def verify(
    token: str, policy: AttestationPolicy, resolver: AttestationKeyResolver, **kwargs: Any
) -> AttestationOutcome:
    return verify_attestation_token(token, policy=policy, key_resolver=resolver, **kwargs)


# -- the one accepted case --------------------------------------------------


def test_a_fully_valid_token_is_the_only_way_to_attest(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    outcome = verify(make_attestation_token(), policy, resolver)

    assert outcome.failure is None, outcome.detail
    assert outcome.verified is True
    assert outcome.trust_level is TrustLevel.HARDWARE_ATTESTED
    assert isinstance(outcome.evidence, AttestationEvidence)
    assert outcome.evidence.image_digest == IMAGE_DIGEST
    assert outcome.evidence.workload_service_account == WORKLOAD_SA
    assert outcome.evidence.issuer == CONFIDENTIAL_SPACE_ISSUER
    assert outcome.evidence.audience == AUDIENCE
    assert outcome.evidence.hardware_model == "GCP_AMD_SEV"
    assert outcome.evidence.debug_status == "disabled-since-boot"
    assert outcome.evidence.expires_at > outcome.evidence.issued_at


def test_verified_at_comes_from_the_supplied_clock(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    moment = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    outcome = verify(make_attestation_token(), policy, resolver, clock=lambda: moment)
    assert outcome.evidence is not None
    assert outcome.evidence.verified_at == moment


def test_evidence_is_frozen(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """Evidence is a record of what was checked; it cannot be edited afterwards."""
    outcome = verify(make_attestation_token(), policy, resolver)
    assert outcome.evidence is not None
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is a subclass detail
        outcome.evidence.image_digest = OTHER_DIGEST  # type: ignore[misc]


# -- rejection: signature ---------------------------------------------------


def test_a_token_signed_by_another_key_is_rejected(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    attacker_key: rsa.RSAPrivateKey,
) -> None:
    token = make_attestation_token(key=_pem(attacker_key))
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.UNTRUSTED_SIGNATURE


def test_editing_the_claims_of_a_real_token_breaks_it(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """The digest is swapped in the payload while the original signature is kept."""
    header, payload, signature = make_attestation_token().split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["submods"]["container"]["image_digest"] = OTHER_DIGEST
    forged_payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode("ascii")
    )

    outcome = verify(f"{header}.{forged_payload}.{signature}", policy, resolver)
    assert outcome.failure is AttestationFailure.UNTRUSTED_SIGNATURE


def test_an_hmac_token_is_refused_before_the_key_is_resolved(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """Algorithm confusion: the attacker must not choose the algorithm."""
    token = make_attestation_token(key="a" * 40, algorithm="HS256")
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.UNSUPPORTED_ALGORITHM
    assert resolver.calls == 0


def test_an_unsigned_token_is_refused(policy: AttestationPolicy, resolver: _Resolver) -> None:
    claims = {"iss": CONFIDENTIAL_SPACE_ISSUER, "aud": AUDIENCE, "sub": "x"}
    token = jwt.encode(claims, key="", algorithm="none")
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.UNSUPPORTED_ALGORITHM
    assert resolver.calls == 0


def test_only_asymmetric_algorithms_are_accepted() -> None:
    assert ACCEPTED_ALGORITHMS == ("RS256",)


def test_a_pem_public_key_from_the_resolver_is_accepted(
    policy: AttestationPolicy,
    rsa_key: rsa.RSAPrivateKey,
    make_attestation_token: MakeAttestationToken,
) -> None:
    """A key source may hand back PEM text rather than a key object.

    PyJWT parses it, so we do too — via `prepare_key`, before verification. This
    is the reason that call exists: without it the PEM never becomes a key, and
    the `verify`-method check below would reject a perfectly good public key.
    """

    class ReturnsPem:
        def __init__(self, pem: str) -> None:
            self._pem = pem

        def signing_key_for(self, token: str) -> Any:
            return self._pem

    pem = (
        rsa_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    outcome = verify(make_attestation_token(), policy, ReturnsPem(pem))
    assert outcome.verified is True, outcome.detail
    assert outcome.trust_level is TrustLevel.HARDWARE_ATTESTED


def test_a_private_key_is_not_a_verification_key(
    policy: AttestationPolicy,
    rsa_key: rsa.RSAPrivateKey,
    make_attestation_token: MakeAttestationToken,
) -> None:
    """The likeliest real misconfiguration of a key source, and it used to crash.

    `prepare_key` accepts a private key, and PyJWT then calls `key.verify(...)`,
    which a private key does not have — an `AttributeError` that is neither a
    `PyJWTError` nor anything else we caught, so it reached the fail-closed
    backstop. Found by the reviewer on round 3 of this ticket.
    """

    class ReturnsThePrivateKey:
        def __init__(self, value: Any) -> None:
            self._value = value

        def signing_key_for(self, token: str) -> Any:
            return self._value

    for value in (rsa_key, _pem(rsa_key)):
        outcome = verify(make_attestation_token(), policy, ReturnsThePrivateKey(value))
        assert outcome.failure is AttestationFailure.UNRESOLVED_SIGNING_KEY, type(value).__name__
        assert outcome.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


def test_an_unresolvable_key_is_a_failure_not_a_skip(
    policy: AttestationPolicy, make_attestation_token: MakeAttestationToken
) -> None:
    class Failing:
        def signing_key_for(self, token: str) -> Any:
            raise RuntimeError("JWKS endpoint unreachable")

    outcome = verify(make_attestation_token(), policy, Failing())
    assert outcome.failure is AttestationFailure.UNRESOLVED_SIGNING_KEY


# -- rejection: nothing escapes as an exception -----------------------------


def test_an_out_of_range_expiry_is_an_outcome_not_a_crash(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """A signed token may still carry `exp: 1e30`.

    Converting that to a datetime raises `OverflowError`, which used to leave
    `verify_attestation_token` as an exception rather than an outcome. Found by
    the reviewer on round 1 of this ticket.
    """
    token = make_attestation_token(overrides={"exp": 1e30})
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.MISSING_CLAIM
    assert outcome.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


def test_an_out_of_range_issued_at_is_an_outcome_not_a_crash(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    token = make_attestation_token(overrides={"iat": 1e30})
    outcome = verify(token, policy, resolver)
    # PyJWT rejects an `iat` in the future before the conversion is reached, so
    # the guard in `_required_timestamp` is defence in depth here rather than
    # the only thing standing between us and an `OverflowError`.
    assert outcome.failure is AttestationFailure.NOT_YET_VALID
    assert outcome.verified is False


@pytest.mark.parametrize("claim", ["exp", "iat"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -1e30])
def test_a_non_finite_timestamp_is_rejected_by_name(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    claim: str,
    value: float,
) -> None:
    """A named failure, not the backstop.

    `int(inf)` raises `OverflowError` inside PyJWT's own `exp`/`iat` validation,
    which is an `ArithmeticError` rather than any `PyJWTError`. Round 2 of this
    ticket's review found it landing in the fail-closed backstop while a guard
    that never ran carried a comment saying it was doing the work.
    """
    outcome = verify(make_attestation_token(overrides={claim: value}), policy, resolver)
    assert outcome.verified is False
    assert outcome.trust_level is TrustLevel.DEVELOPMENT_ISOLATION
    assert outcome.failure is not AttestationFailure.VERIFICATION_ERROR, outcome.detail


@pytest.mark.parametrize("claim", ["exp", "iat"])
def test_an_infinite_timestamp_is_reported_as_a_missing_claim(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    claim: str,
) -> None:
    token = make_attestation_token(overrides={claim: float("inf")})
    assert verify(token, policy, resolver).failure is AttestationFailure.MISSING_CLAIM


UNUSABLE_KEYS: list[Any] = ["a-symmetric-secret", 12345, None, b"\x00\x01", object()]


@pytest.mark.parametrize("key", UNUSABLE_KEYS)
def test_a_key_that_is_not_a_key_is_an_outcome_not_a_crash(
    policy: AttestationPolicy, make_attestation_token: MakeAttestationToken, key: Any
) -> None:
    """`InvalidKeyError` is a sibling of `InvalidTokenError`, not a subclass.

    An `oct` JWKS entry or a misconfigured pinned key used to escape the handler
    entirely. Non-key objects raise `TypeError` from PyJWT for the same reason.
    Found by the reviewer on round 1 of this ticket.
    """

    class ReturnsRubbish:
        def signing_key_for(self, token: str) -> Any:
            return key

    outcome = verify(make_attestation_token(), policy, ReturnsRubbish())
    assert outcome.failure is AttestationFailure.UNRESOLVED_SIGNING_KEY
    assert outcome.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


def test_an_unanticipated_exception_still_fails_closed(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """The backstop. Nothing in verification may surface as an exception."""

    def broken_clock() -> datetime:
        raise RuntimeError("the clock is on fire")

    outcome = verify(make_attestation_token(), policy, resolver, clock=broken_clock)
    assert outcome.failure is AttestationFailure.VERIFICATION_ERROR
    assert outcome.trust_level is TrustLevel.DEVELOPMENT_ISOLATION
    assert outcome.evidence is None
    assert "RuntimeError" in (outcome.detail or "")


ADVERSARIAL_CLAIMS: list[dict[str, Any]] = [
    {"exp": 1e30},
    {"iat": 1e30},
    {"exp": -1e30},
    {"iat": float("nan")},
    {"exp": float("inf")},
    {"exp": True},
    {"iat": None},
    {"sub": ["a", "b"]},
    {"sub": {"nested": "object"}},
    {"iss": ""},
    {"aud": [AUDIENCE, "someone-else"]},
    {"aud": [AUDIENCE, AUDIENCE]},
    {"aud": []},
    {"google_service_account": 42},
    {"hwmodel": None},
    {"swname": {"a": 1}},
    {"dbgstat": ["disabled-since-boot"]},
    {"nbf": None},
    {"nbf": []},
    {"nbf": {}},
    {"nbf": "soon"},
    {"nbf": float("inf")},
    {"nbf": float("nan")},
    {"submods": None},
    {"submods": []},
    {"submods": {"container": None}},
    {"submods": {"container": []}},
    {"submods": {"container": {"image_digest": 5, "image_reference": "r"}}},
    {"submods": {"container": {"image_digest": IMAGE_DIGEST, "image_reference": None}}},
    {"submods": {"container": {"image_digest": IMAGE_DIGEST.upper(), "image_reference": "r"}}},
    {"submods": {"container": {"image_digest": f"  {IMAGE_DIGEST}  ", "image_reference": "r"}}},
    {"submods": {"container": {"image_digest": f"{IMAGE_DIGEST}\n", "image_reference": "r"}}},
    {"hwmodel": " GCP_AMD_SEV "},
    {"dbgstat": "  disabled-since-boot  "},
    {"google_service_account": f" {WORKLOAD_SA} "},
    {"swname": "CONFIDENTIAL_SPACE\n"},
]


@pytest.mark.parametrize("overrides", ADVERSARIAL_CLAIMS)
def test_no_claim_shape_escapes_as_an_exception(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    overrides: dict[str, Any],
) -> None:
    """The whole claim-extraction path, swept for escapes rather than crashes.

    Each of these is a signed token — the signature is genuine — carrying a claim
    of a type or magnitude the extraction path has to survive. Verification must
    return an outcome for every one of them. Two of these shapes are the round-1
    findings; the rest are the same question asked of every other conversion.
    """
    outcome = verify(make_attestation_token(overrides=overrides), policy, resolver)
    assert outcome.trust_level is TrustLevel.DEVELOPMENT_ISOLATION
    assert outcome.evidence is None
    assert outcome.failure is not None
    # The point of the sweep. Without this line it cannot tell "checked and
    # rejected" from "crashed and swallowed by the backstop", and round 2 of this
    # ticket's review found it passing on exactly that. There is no allowlist:
    # every shape here must produce a named failure. If a future shape genuinely
    # belongs in the backstop, it goes in a separate test that says so.
    assert outcome.failure is not AttestationFailure.VERIFICATION_ERROR, (
        f"{overrides} reached the fail-closed backstop instead of a named check: {outcome.detail}"
    )


def test_the_adversarial_sweep_is_not_empty() -> None:
    assert len(ADVERSARIAL_CLAIMS) >= 20


#: Values a claim must survive carrying. Types, magnitudes and empties — the
#: shapes that make a converter or a comparison raise rather than decide.
HOSTILE_VALUES: list[Any] = [
    None,
    [],
    {},
    "",
    "   ",
    "soon",
    0,
    -1,
    True,
    False,
    1e30,
    -1e30,
    float("inf"),
    float("-inf"),
    float("nan"),
    ["a", "b"],
    {"nested": 1},
    3.5,
    [{"deeply": ["nested"]}],
    "\u0000",
    "0x10",
]

#: Only JSON-representable values appear above: a JWT payload is JSON, so a
#: `bytes` claim cannot arrive in a token and testing one would be testing our
#: own fixture rather than the verifier.

#: Every claim this module reads, plus every RFC 7519 registered claim — whether
#: or not we read it, because PyJWT validates several of them on our behalf and
#: a claim we never touch can still crash its validator. `nbf` is the reason this
#: list exists: it was absent from a hand-written sweep, PyJWT calls `int()` on
#: it, and `nbf: None` reached the fail-closed backstop for two rounds.
#: `test_every_claim_the_module_reads_has_hostile_shapes` derives the same set
#: from the source and fails if this one falls behind, so it is not maintained
#: from memory.
MATRIX_CLAIMS = [
    "iss",
    "sub",
    "aud",
    "exp",
    "nbf",
    "iat",
    "jti",
    "google_service_account",
    "hwmodel",
    "swname",
    "dbgstat",
    "submods",
]

#: Claims read from inside `submods.container` rather than the top level.
NESTED_CLAIMS = ["container", "image_digest", "image_reference"]

#: RFC 7519 §4.1, in full.
RFC_REGISTERED_CLAIMS = frozenset({"iss", "sub", "aud", "exp", "nbf", "iat", "jti"})


#: Header parameters the module reads. Not claims — the header is unsigned input
#: read *before* the signature is checked, which makes it the earliest thing an
#: attacker controls.
HEADER_PARAMS = ["alg"]

#: Dict receivers `attestation.py` may call `.get(...)` on, and what each one is.
#: A new receiver name fails the coverage test rather than being silently taken
#: for a claim dict.
KNOWN_RECEIVERS = {"claims": "claim", "container": "claim", "header": "header"}


def _names_read_by_the_module() -> tuple[set[str], set[str]]:
    """Claim names and header parameter names, taken from the module's own source.

    Enumerated from the AST rather than from recall: `<receiver>.get("x")`, the
    second argument of `_required_str` / `_required_timestamp`, and the members
    of `REQUIRED_CLAIMS`. The receiver name decides whether a name is a claim or
    a header parameter, and an unrecognised receiver is an error.
    """
    tree = ast.parse((SRC / "mcpforge" / "execution" / "attestation.py").read_text())
    claims: set[str] = set()
    headers: set[str] = set()
    unknown: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    receiver = func.value.id if isinstance(func.value, ast.Name) else "<expr>"
                    kind = KNOWN_RECEIVERS.get(receiver)
                    if kind == "claim":
                        claims.add(first.value)
                    elif kind == "header":
                        headers.add(first.value)
                    else:
                        unknown.add(f"{receiver}.get({first.value!r})")
            if (
                isinstance(func, ast.Name)
                and func.id in {"_required_str", "_required_timestamp"}
                and len(node.args) >= 2
            ):
                second = node.args[1]
                if isinstance(second, ast.Constant) and isinstance(second.value, str):
                    claims.add(second.value)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "REQUIRED_CLAIMS" for t in node.targets
        ):
            claims.update(
                e.value
                for e in ast.walk(node)
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )

    assert not unknown, f"unrecognised dict receiver — claim or header? {sorted(unknown)}"
    return claims, headers


def test_every_claim_the_module_reads_has_hostile_shapes() -> None:
    """The anti-recall check: the matrix is derived from the code, not remembered.

    `nbf` was missed on rounds 1 and 2 because `ADVERSARIAL_CLAIMS` was written
    from memory, and PyJWT validates `nbf` even though our code never names it.
    This test fails if a claim is added to the module — or is in RFC 7519 — and
    has no hostile shapes exercised against it.
    """
    claims, headers = _names_read_by_the_module()
    assert len(claims) >= 8, "the AST scan found almost nothing; it would pass vacuously"
    assert headers, "the AST scan found no header parameter; it is reading the wrong thing"

    covered = set(MATRIX_CLAIMS) | set(NESTED_CLAIMS)
    missing = (claims | RFC_REGISTERED_CLAIMS) - covered
    assert not missing, f"claims with no hostile shapes in the matrix: {sorted(missing)}"

    missing_headers = headers - set(HEADER_PARAMS)
    assert not missing_headers, f"header parameters with no hostile shapes: {missing_headers}"


@pytest.mark.parametrize("param", HEADER_PARAMS)
def test_no_header_shape_reaches_the_backstop(
    policy: AttestationPolicy, resolver: _Resolver, param: str
) -> None:
    """The header is read before the signature is verified, so it is checked too.

    A hostile `alg` must be refused by the allowlist rather than crash it — an
    unhashable value in a membership test, for instance.
    """
    for value in HOSTILE_VALUES:
        # Assembled by hand: PyJWT's encoder resolves `alg` to an implementation
        # and refuses to mint these, while an attacker writes the header itself.
        # The signature is deliberately junk — the algorithm allowlist runs
        # before any signature check, which is the property under test.
        header = _b64url(json.dumps({param: value, "typ": "JWT"}).encode())
        payload = _b64url(json.dumps({"sub": "x"}).encode())
        token = f"{header}.{payload}.{_b64url(b'not-a-signature')}"
        outcome = verify(token, policy, resolver)
        assert outcome.failure is not AttestationFailure.VERIFICATION_ERROR, (
            f"header {param}={value!r} reached the backstop: {outcome.detail}"
        )
        assert outcome.verified is False


@pytest.mark.parametrize("claim", MATRIX_CLAIMS)
def test_no_top_level_claim_shape_reaches_the_backstop(
    policy: AttestationPolicy,
    resolver: _Resolver,
    sign_claims: SignClaims,
    base_claims: dict[str, Any],
    claim: str,
) -> None:
    """Every claim crossed with every hostile value, on genuinely signed tokens.

    The tokens are signed through the JWS layer so that shapes `jwt.encode`
    refuses to mint — `iss: 17` among them — can still be tested, because a
    hostile issuer is under no obligation to use PyJWT's encoder. Some of these
    tokens legitimately still verify (`jti: 0` changes nothing), so the assertion
    is not "rejected" but "decided": never the fail-closed backstop, and never an
    exception.
    """
    for value in HOSTILE_VALUES:
        claims = {**base_claims, claim: value}
        outcome = verify(sign_claims(claims), policy, resolver)
        assert outcome.failure is not AttestationFailure.VERIFICATION_ERROR, (
            f"{claim}={value!r} reached the fail-closed backstop: {outcome.detail}"
        )
        if outcome.verified:
            assert outcome.evidence is not None


@pytest.mark.parametrize("claim", NESTED_CLAIMS)
def test_no_nested_claim_shape_reaches_the_backstop(
    policy: AttestationPolicy,
    resolver: _Resolver,
    sign_claims: SignClaims,
    base_claims: dict[str, Any],
    claim: str,
) -> None:
    for value in HOSTILE_VALUES:
        claims = json.loads(json.dumps(base_claims))
        if claim == "container":
            claims["submods"]["container"] = value
        else:
            claims["submods"]["container"][claim] = value
        outcome = verify(sign_claims(claims), policy, resolver)
        assert outcome.failure is not AttestationFailure.VERIFICATION_ERROR, (
            f"submods.container.{claim}={value!r} reached the backstop: {outcome.detail}"
        )


def test_hostile_values_cover_the_shapes_that_have_bitten_us() -> None:
    """Self-guard: an empty or defanged matrix would pass everything above."""
    assert len(HOSTILE_VALUES) >= 15
    assert None in HOSTILE_VALUES  # nbf: None — round 3
    assert float("inf") in HOSTILE_VALUES  # exp: inf — round 2
    assert 1e30 in HOSTILE_VALUES  # exp: 1e30 — round 1
    assert [] in HOSTILE_VALUES and {} in HOSTILE_VALUES
    assert len(MATRIX_CLAIMS) >= 12


def test_nothing_a_caller_can_supply_reaches_the_backstop(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    attacker_key: rsa.RSAPrivateKey,
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    """Every input this module can be handed: tokens *and* key sources.

    An earlier version of this test was a strict superset of the adversarial
    sweep by four malformed strings, and used only the good resolver — so it
    detected nothing the sweep did not, and was blind to the private-key defect
    the reviewer found on round 3 despite a docstring claiming it covered
    "the unusable keys". It now drives every bad token, every padded-claim
    variant and every unusable key, each against a resolver that is genuinely
    misconfigured in that way.
    """

    class ReturnsFixed:
        def __init__(self, value: Any) -> None:
            self._value = value

        def signing_key_for(self, token: str) -> Any:
            return self._value

    class Failing:
        def signing_key_for(self, token: str) -> Any:
            raise RuntimeError("JWKS endpoint unreachable")

    tokens = [
        *_all_bad_tokens(make_attestation_token, attacker_key),
        make_attestation_token(),
        *(make_attestation_token(overrides=o) for o in ADVERSARIAL_CLAIMS),
        *(
            make_attestation_token(image_digest=d)
            for d in (f"  {IMAGE_DIGEST}  ", IMAGE_DIGEST[7:])
        ),
        make_attestation_token(hardware_model=" GCP_AMD_SEV "),
        make_attestation_token(debug_status="  disabled-since-boot  "),
        make_attestation_token(service_account=f" {WORKLOAD_SA} "),
    ]
    resolvers: list[Any] = [
        resolver,
        Failing(),
        ReturnsFixed(rsa_key),
        ReturnsFixed(_pem(rsa_key)),
        *(ReturnsFixed(k) for k in UNUSABLE_KEYS),
    ]
    assert len(tokens) >= 40 and len(resolvers) >= 9

    for key_source in resolvers:
        for token in tokens:
            outcome = verify(token, policy, key_source)
            assert outcome.failure is not AttestationFailure.VERIFICATION_ERROR, (
                f"{type(key_source).__name__} + {token[:24]!r}: {outcome.detail}"
            )


# -- rejection: time --------------------------------------------------------


def test_an_expired_token_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    token = make_attestation_token(expires_in=-3600, issued_ago=7200)
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.EXPIRED


def test_clock_skew_tolerance_is_bounded(
    resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """Skew is a small allowance, not an open door."""
    policy = AttestationPolicy(
        audience=AUDIENCE,
        image_digest=IMAGE_DIGEST,
        workload_service_account=WORKLOAD_SA,
        max_clock_skew_seconds=30,
    )
    assert verify(make_attestation_token(expires_in=-5), policy, resolver).verified is True
    assert verify(make_attestation_token(expires_in=-90), policy, resolver).verified is False


def test_a_token_from_the_future_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    token = make_attestation_token(not_before_in=3600)
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.NOT_YET_VALID


# -- rejection: addressing and identity -------------------------------------


def test_a_token_minted_for_someone_else_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    token = make_attestation_token(audience="some-other-consumer")
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.WRONG_AUDIENCE


def test_a_token_from_another_issuer_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    token = make_attestation_token(issuer="https://attestation.evil.example.com")
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.WRONG_ISSUER


def test_a_token_addressed_to_more_than_one_audience_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """PyJWT accepts a list `aud` containing ours. For a per-run nonce, we do not.

    A token addressed to us *and* to somebody else is a token somebody else also
    holds. Found by the adversarial sweep added in round 1.
    """
    token = make_attestation_token(overrides={"aud": [AUDIENCE, "another-consumer"]})
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.WRONG_AUDIENCE


def test_a_single_element_audience_list_is_still_ours(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    outcome = verify(make_attestation_token(overrides={"aud": [AUDIENCE]}), policy, resolver)
    assert outcome.verified is True
    assert outcome.evidence is not None
    assert outcome.evidence.audience == AUDIENCE


def test_a_non_canonical_digest_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """No case folding on the token side: a digest is canonical or it is refused."""
    for shape in (
        IMAGE_DIGEST.upper(),
        "sha256:" + "AB" * 32,
        IMAGE_DIGEST.removeprefix("sha256:"),
        IMAGE_DIGEST.replace("sha256", "sha512"),
    ):
        outcome = verify(make_attestation_token(image_digest=shape), policy, resolver)
        assert outcome.failure is AttestationFailure.UNEXPECTED_IMAGE_DIGEST, shape


def test_a_different_image_digest_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """An authentic token from the wrong image proves nothing about our code."""
    token = make_attestation_token(image_digest=OTHER_DIGEST)
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.UNEXPECTED_IMAGE_DIGEST


def test_digest_matching_is_exact_not_a_prefix(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    token = make_attestation_token(image_digest=IMAGE_DIGEST[:-4] + "0000")
    assert verify(token, policy, resolver).failure is AttestationFailure.UNEXPECTED_IMAGE_DIGEST


@pytest.mark.parametrize(
    ("field", "padded", "expected"),
    [
        ("image_digest", f"  {IMAGE_DIGEST}  ", AttestationFailure.UNEXPECTED_IMAGE_DIGEST),
        ("image_digest", f"{IMAGE_DIGEST}\n", AttestationFailure.UNEXPECTED_IMAGE_DIGEST),
        ("hardware_model", " GCP_AMD_SEV ", AttestationFailure.UNEXPECTED_HARDWARE),
        ("software_name", "CONFIDENTIAL_SPACE ", AttestationFailure.UNEXPECTED_HARDWARE),
        ("debug_status", "  disabled-since-boot  ", AttestationFailure.DEBUG_MODE_ENABLED),
        ("service_account", f" {WORKLOAD_SA} ", AttestationFailure.UNEXPECTED_WORKLOAD),
    ],
)
def test_a_padded_claim_is_not_silently_normalised(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    field: str,
    padded: str,
    expected: AttestationFailure,
) -> None:
    """ "Matched exactly" has to mean exactly.

    An earlier version stripped every string claim, so `"  sha256:<hex>  "` and
    `" GCP_AMD_SEV "` verified as `HARDWARE_ATTESTED` while the comment beside
    the comparison and `03_SECURITY_ACCESS.md` §2 both said the match was exact.
    Found by the reviewer on round 2. Not exploitable, but this is the T7
    control and the written claim has to match the code.
    """
    outcome = verify(make_attestation_token(**{field: padded}), policy, resolver)
    assert outcome.failure is expected
    assert outcome.verified is False


def test_a_whitespace_only_claim_is_missing_not_present(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """The one normalisation that remains, stated: blank is absent."""
    outcome = verify(make_attestation_token(hardware_model="   "), policy, resolver)
    assert outcome.failure is AttestationFailure.MISSING_CLAIM


def test_a_different_workload_identity_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    token = make_attestation_token(service_account="someone-else@example.iam.gserviceaccount.com")
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.UNEXPECTED_WORKLOAD


# -- rejection: platform ----------------------------------------------------


def test_a_non_confidential_platform_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    token = make_attestation_token(hardware_model="GCP_SHIELDED_VM")
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.UNEXPECTED_HARDWARE


def test_a_different_software_stack_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    token = make_attestation_token(software_name="SOMETHING_ELSE")
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.UNEXPECTED_HARDWARE


def test_a_debuggable_vm_is_rejected(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    """A debugger attached since boot defeats the memory-encryption guarantee."""
    token = make_attestation_token(debug_status="enabled-since-boot")
    outcome = verify(token, policy, resolver)
    assert outcome.failure is AttestationFailure.DEBUG_MODE_ENABLED


# -- rejection: shape -------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_no_token_is_not_a_pass(policy: AttestationPolicy, resolver: _Resolver, blank: str) -> None:
    assert verify(blank, policy, resolver).failure is AttestationFailure.NO_TOKEN


def test_garbage_is_rejected_as_malformed(policy: AttestationPolicy, resolver: _Resolver) -> None:
    assert verify("not-a-jwt", policy, resolver).failure is AttestationFailure.MALFORMED_TOKEN


@pytest.mark.parametrize("claim", ["iss", "aud", "exp", "iat", "sub"])
def test_a_missing_registered_claim_is_rejected(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    claim: str,
) -> None:
    """Deliberate redundancy, recorded because a mutation survives here.

    Deleting `options={"require": REQUIRED_CLAIMS}` from `jwt.decode` keeps this
    green: PyJWT still demands `iss` and `aud` when they are pinned, and
    `_evidence_from_claims` independently requires `sub`, `iat` and `exp`. The
    `require` option is kept anyway — it is the check that stops a token *with no
    expiry* being treated as eternally valid if the evidence extraction is ever
    reordered. Same trade as the redundant writer guards in
    `02_ARCHITECTURE.md`: one comparison guarding the product's most
    misrepresentable claim is worth a surviving mutation.
    """
    outcome = verify(make_attestation_token(omit=(claim,)), policy, resolver)
    assert outcome.failure in {
        AttestationFailure.MISSING_CLAIM,
        AttestationFailure.WRONG_AUDIENCE,
        AttestationFailure.WRONG_ISSUER,
    }
    assert outcome.verified is False


@pytest.mark.parametrize(
    "claim", ["google_service_account", "hwmodel", "swname", "dbgstat", "submods"]
)
def test_a_missing_attestation_claim_is_rejected(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    claim: str,
) -> None:
    outcome = verify(make_attestation_token(omit=(claim,)), policy, resolver)
    assert outcome.failure is AttestationFailure.MISSING_CLAIM


@pytest.mark.parametrize(
    "overrides",
    [
        {"submods": "container"},
        {"submods": {}},
        {"submods": {"container": {"image_reference": "x"}}},
        {"submods": {"container": {"image_digest": IMAGE_DIGEST}}},
        {"hwmodel": 7},
        {"dbgstat": ""},
        {"exp": "soon"},
    ],
)
def test_claims_of_the_wrong_shape_are_rejected(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    overrides: dict[str, Any],
) -> None:
    outcome = verify(make_attestation_token(overrides=overrides), policy, resolver)
    assert outcome.verified is False
    assert outcome.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


# -- the acceptance criterion: failure never upgrades -----------------------


def _all_bad_tokens(make: MakeAttestationToken, attacker: rsa.RSAPrivateKey) -> list[str]:
    return [
        "",
        "not-a-jwt",
        "a.b.c",
        make(key=_pem(attacker)),
        make(key="s" * 40, algorithm="HS256"),
        make(expires_in=-3600),
        make(not_before_in=600),
        make(audience="elsewhere"),
        make(issuer="https://evil.example.com"),
        make(image_digest=OTHER_DIGEST),
        make(service_account="other@example.iam.gserviceaccount.com"),
        make(hardware_model="GCP_SHIELDED_VM"),
        make(software_name="OTHER"),
        make(debug_status="enabled-since-boot"),
        make(omit=("sub",)),
        make(omit=("submods",)),
    ]


def test_every_rejection_is_development_isolation_with_a_reason(
    policy: AttestationPolicy,
    resolver: _Resolver,
    make_attestation_token: MakeAttestationToken,
    attacker_key: rsa.RSAPrivateKey,
) -> None:
    """F8-01: verification failure yields DEVELOPMENT_ISOLATION, never an upgrade."""
    tokens = _all_bad_tokens(make_attestation_token, attacker_key)
    assert len(tokens) >= 16  # self-guard: an empty sweep would pass vacuously

    for token in tokens:
        outcome = verify(token, policy, resolver)
        assert outcome.trust_level is TrustLevel.DEVELOPMENT_ISOLATION, token[:40]
        assert outcome.evidence is None, token[:40]
        assert outcome.failure is not None, token[:40]
        assert outcome.detail


def test_an_outcome_cannot_be_upgraded_without_evidence() -> None:
    """The invariant that stops a caller assembling a verified-looking result."""
    with pytest.raises(ValueError, match="cannot raise the trust level"):
        AttestationOutcome(trust_level=TrustLevel.HARDWARE_ATTESTED, evidence=None)


def test_an_outcome_must_say_why_it_failed() -> None:
    with pytest.raises(ValueError, match="must record why"):
        AttestationOutcome(trust_level=TrustLevel.DEVELOPMENT_ISOLATION)


def test_an_outcome_cannot_hold_evidence_and_a_failure(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    good = verify(make_attestation_token(), policy, resolver)
    assert good.evidence is not None
    with pytest.raises(ValueError, match="both hold evidence"):
        AttestationOutcome(
            trust_level=TrustLevel.DEVELOPMENT_ISOLATION,
            evidence=good.evidence,
            failure=AttestationFailure.EXPIRED,
        )


# -- policy is not allowed to be permissive by omission ---------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"audience": ""},
        {"audience": "   "},
        {"issuer": ""},
        {"workload_service_account": ""},
        {"image_digest": ""},
        {"image_digest": "latest"},
        {"image_digest": "sha256:short"},
        {"image_digest": "md5:" + "ab" * 32},
        {"allowed_hardware_models": frozenset()},
        {"required_software_name": ""},
        {"required_debug_status": ""},
        {"max_clock_skew_seconds": -1},
    ],
)
def test_an_underspecified_policy_is_refused(changes: dict[str, Any]) -> None:
    kwargs: dict[str, Any] = {
        "audience": AUDIENCE,
        "image_digest": IMAGE_DIGEST,
        "workload_service_account": WORKLOAD_SA,
        **changes,
    }
    with pytest.raises(AttestationPolicyError):
        AttestationPolicy(**kwargs)


# -- the verification interface ---------------------------------------------


def test_the_verifier_satisfies_the_port(policy: AttestationPolicy, resolver: _Resolver) -> None:
    verifier = ConfidentialSpaceAttestationVerifier(policy=policy, key_resolver=resolver)
    assert isinstance(verifier, AttestationVerifier)
    assert isinstance(resolver, AttestationKeyResolver)


def test_the_verifier_applies_the_same_checks(
    policy: AttestationPolicy, resolver: _Resolver, make_attestation_token: MakeAttestationToken
) -> None:
    verifier = ConfidentialSpaceAttestationVerifier(policy=policy, key_resolver=resolver)
    assert verifier.verify(make_attestation_token()).verified is True
    assert verifier.verify(make_attestation_token(image_digest=OTHER_DIGEST)).failure is (
        AttestationFailure.UNEXPECTED_IMAGE_DIGEST
    )


def test_the_jwks_resolver_needs_a_real_endpoint() -> None:
    """No default URL: an unverified endpoint constant is a guess in a control."""
    with pytest.raises(ValueError, match="JWKS URL"):
        JwksAttestationKeyResolver("")


def test_the_jwks_resolver_returns_the_key_from_the_client(
    rsa_key: rsa.RSAPrivateKey, private_pem: str
) -> None:
    class Client:
        def get_signing_key_from_jwt(self, token: str) -> Any:
            class Key:
                key = rsa_key.public_key()

            return Key()

    resolver = JwksAttestationKeyResolver("https://example.invalid/jwks", client=Client())
    token = jwt.encode({"sub": "x"}, private_pem, algorithm="RS256")
    assert resolver.signing_key_for(token) == rsa_key.public_key()


# -- exactly one producer, checked against the source -----------------------


def _enclosing_function(tree: ast.Module, lineno: int) -> str:
    """Innermost function containing this line, or '<module>' if none."""
    best = "<module>"
    best_span = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= lineno <= end:
            span = end - node.lineno
            if best_span is None or span < best_span:
                best, best_span = node.name, span
    return best


def _enum_member_lines(tree: ast.Module) -> set[int]:
    """Line numbers of the `HARDWARE_ATTESTED = "HARDWARE_ATTESTED"` declaration."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "TrustLevel":
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign | ast.AnnAssign):
                for sub in ast.walk(stmt):
                    if isinstance(sub, ast.Name) and sub.id == "HARDWARE_ATTESTED":
                        lines.add(stmt.lineno)
    return lines


def _hardware_attested_sites() -> list[tuple[str, str, int]]:
    """Every mention of the attested trust level in backend source.

    Matches the attribute (`TrustLevel.HARDWARE_ATTESTED`), the bare name, and
    the string literal — so `TrustLevel("HARDWARE_ATTESTED")` is caught too.
    Reads the AST rather than the text, so prose explaining the rule does not
    trip it while real usage does.
    """
    sites: list[tuple[str, str, int]] = []
    for path in python_files():
        tree = ast.parse(path.read_text())
        skip = _enum_member_lines(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                hit = node.attr == "HARDWARE_ATTESTED"
            elif isinstance(node, ast.Name):
                hit = node.id == "HARDWARE_ATTESTED"
            elif isinstance(node, ast.Constant):
                hit = node.value == "HARDWARE_ATTESTED"
            else:
                continue
            if not hit or node.lineno in skip:
                continue
            rel = str(path.relative_to(SRC))
            sites.append((rel, _enclosing_function(tree, node.lineno), node.lineno))
    return sites


def test_exactly_one_function_can_produce_the_attested_trust_level() -> None:
    """F8-01 acceptance criterion, checked against the source tree itself.

    `HARDWARE_ATTESTED` is assignable from exactly one function. If someone adds
    a second producer — a config branch, a second executor, a helper that returns
    it — this fails and names the file and line.

    **Limit, stated rather than papered over.** This matches on names, exactly
    like the approval sweep in `02_ARCHITECTURE.md` §6. It catches
    straightforwardly-written code; it cannot defeat deliberate indirection such
    as `getattr(TrustLevel, some_name)` or `TrustLevel(value_from_config)`. No
    name-based check can. The guarantee is the conjunction of this sweep and the
    behavioural tests above, which show that every failure path returns
    `DEVELOPMENT_ISOLATION` and that an outcome cannot be upgraded without
    evidence.
    """
    scanned = python_files()
    assert len(scanned) > 20, "the sweep scanned almost nothing; it would pass vacuously"

    sites = _hardware_attested_sites()
    assert sites, "found no producer at all — the sweep is not matching what it claims to"

    producers = {(path, function) for path, function, _ in sites}
    assert producers == {("mcpforge/execution/attestation.py", "verify_attestation_token")}, (
        "the attested trust level is produced in more than one place:\n"
        + "\n".join(f"{p}:{line}: in {fn}" for p, fn, line in sorted(sites))
    )


def test_evidence_is_constructed_in_exactly_one_place() -> None:
    """Evidence is what raises the trust level, so it has the same rule as the enum.

    `AttestationEvidence` is an ordinary dataclass and any module could build
    one; this asserts that none does. Without it, the docstring claim that
    evidence "exists only as the result of a fully successful
    verify_attestation_token" would be a statement about intent rather than
    about the code. Same name-based limit as the producer sweep above.
    """
    sites: list[str] = []
    for path in python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AttestationEvidence"
            ):
                sites.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert len(sites) == 1, f"evidence is constructed in more than one place: {sites}"
    assert sites[0].startswith("mcpforge/execution/attestation.py")


def test_the_producer_is_the_verification_function_itself() -> None:
    """Not a helper it delegates to: the upgrade sits after the checks, in one body."""
    path = SRC / "mcpforge" / "execution" / "attestation.py"
    tree = ast.parse(path.read_text())
    functions = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "verify_attestation_token"
    ]
    assert len(functions) == 1

    returns = [n for n in ast.walk(functions[0]) if isinstance(n, ast.Return)]
    upgrading = [
        n
        for n in returns
        if any(
            isinstance(sub, ast.Attribute) and sub.attr == "HARDWARE_ATTESTED"
            for sub in ast.walk(n)
        )
    ]
    assert len(upgrading) == 1, "there should be exactly one upgrading return"
    # It is the last statement: everything before it is a check that can reject.
    assert upgrading[0] is functions[0].body[-1]


def test_no_backend_module_obtains_an_attestation_token_yet() -> None:
    """F8-02 is BLOCKED (B-04) and is not simulated.

    Nothing calls the verifier in production, so nothing reports attestation.
    When `F8-02` lands with real infrastructure, this test is replaced by that
    ticket's integration test — it is not deleted quietly.
    """
    callers: list[str] = []
    for path in python_files():
        if path.name == "attestation.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else ""
                )
                if name in {"verify_attestation_token", "verify"} and "attestation" in ast.dump(
                    node
                ):
                    callers.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not callers, "an attestation path exists but F8-02 is blocked:\n" + "\n".join(callers)
