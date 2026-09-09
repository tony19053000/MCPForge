"""Attestation evidence and verification — F8-01. The T7 control.

`03_SECURITY_ACCESS.md` §2 and `02_ARCHITECTURE.md` §8: trust is an enum, and
`HARDWARE_ATTESTED` may be assigned **only** by code that has fetched and
cryptographically verified an attestation token against the expected workload
identity and image digest. This module is where that definition lives, so that
"verified" means one specific, testable thing before anything can claim it.

Three rules hold here. Each names the test that fails if it is violated, because
three review rounds of this ticket each found a comment asserting a property the
code did not have:

1. **One producer.** `verify_attestation_token` is the only function in the
   backend that names `HARDWARE_ATTESTED` outside the enum declaration, and
   `_evidence_from_claims` the only code that constructs an `AttestationEvidence`
   — evidence is what raises the trust level, so it carries the same rule.
   `test_exactly_one_function_can_produce_the_attested_trust_level` and
   `test_evidence_is_constructed_in_exactly_one_place` sweep the AST of every
   backend module. Both match on names: they catch straightforwardly-written
   code and do not defeat deliberate indirection such as
   `getattr(TrustLevel, name)`.
2. **Failure is never optimistic, and never escapes.** Every rejection returns an
   `AttestationOutcome` whose trust level is `DEVELOPMENT_ISOLATION` and whose
   evidence is `None` — `test_every_rejection_is_development_isolation_with_a_reason`
   — and no such outcome can be constructed with a raised trust level, enforced
   in `AttestationOutcome.__post_init__` and pinned by
   `test_an_outcome_cannot_be_upgraded_without_evidence`. There is no partial
   credit, no "assume verified on a network error", and no configuration flag
   that skips a check.

   `verify_attestation_token` raises nothing: every anticipated failure becomes
   an outcome with a **named** reason, and a fail-closed backstop yields
   `VERIFICATION_ERROR` for anything else. Four real escapes were found in
   review, all now named: an out-of-range `exp` (`OverflowError` from
   `datetime.fromtimestamp`); an unparseable signing key (`InvalidKeyError`, a
   *sibling* of `InvalidTokenError` rather than a subclass); a non-finite
   `exp`/`iat` (`OverflowError` from `int()` inside PyJWT's own validation); and
   a non-numeric `nbf` (`TypeError` from the same place, on a claim this module
   never names and PyJWT validates anyway).

   What is demonstrated, and its bound: no input the test matrix can construct
   reaches the backstop — every RFC 7519 registered claim and every claim this
   module reads, each crossed with a hostile-value list, plus every header
   parameter, plus every unusable key shape
   (`test_no_top_level_claim_shape_reaches_the_backstop`,
   `test_no_nested_claim_shape_reaches_the_backstop`,
   `test_no_header_shape_reaches_the_backstop`,
   `test_nothing_a_caller_can_supply_reaches_the_backstop`). The claim is
   bounded by that enumeration, and the enumeration is derived from this file's
   own AST by `test_every_claim_the_module_reads_has_hostile_shapes` rather than
   from recall — `nbf` was missed twice precisely because a list was written
   from memory.
3. **No token source exists.** This module verifies a token it is handed. It
   never obtains one. Fetching a real Confidential Space token is `F8-02`, which
   is `BLOCKED` on real GCP infrastructure (blocker B-04) and is not simulated.
   Nothing in MCPForge calls `verify_attestation_token` in production, asserted
   by `test_no_backend_module_obtains_an_attestation_token_yet`, so nothing
   reports `HARDWARE_ATTESTED`.

**What this module does not prove.** Verifying a token proves the token is
authentic and describes the expected workload. It cannot prove the token was
issued for *this* process rather than replayed from another — that is what the
caller-supplied `audience` nonce is for, and binding it is `F8-02`'s job.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import jwt

#: Google's Confidential Space attestation issuer. A token from any other issuer
#: is rejected; the issuer is part of the policy so a test can use its own.
CONFIDENTIAL_SPACE_ISSUER = "https://confidentialcomputing.googleapis.com"

#: Asymmetric only. `none`, and every HMAC algorithm, are rejected before the key
#: is even resolved — an attacker who can choose the algorithm can forge. The
#: ordering is the point and is pinned by `resolver.calls == 0` in
#: `test_an_hmac_token_is_refused_before_the_key_is_resolved`.
ACCEPTED_ALGORITHMS: tuple[str, ...] = ("RS256",)

#: Claims a token must carry. A missing one is a rejection, never a default.
REQUIRED_CLAIMS: tuple[str, ...] = ("iss", "aud", "exp", "iat", "sub")

#: Confidential Space reports the hardware root of trust in `hwmodel`.
DEFAULT_ALLOWED_HARDWARE_MODELS = frozenset(
    {"GCP_AMD_SEV", "GCP_AMD_SEV_ES", "GCP_AMD_SEV_SNP", "GCP_INTEL_TDX"}
)

_DIGEST_SHAPE = re.compile(r"^sha256:[0-9a-f]{64}$")


class TrustLevel(StrEnum):
    """What the execution boundary actually is. The UI renders this, not a boolean.

    There is no configuration flag, environment variable or test fixture that can
    produce the attested member without a verified attestation token. It is
    produced by exactly one function in this module.
    """

    DEVELOPMENT_ISOLATION = "DEVELOPMENT_ISOLATION"
    HARDWARE_ATTESTED = "HARDWARE_ATTESTED"


class AttestationFailure(StrEnum):
    """Why verification failed. Recorded, surfaced, and never rounded up."""

    # S105 reads "TOKEN" as a credential name; these are failure reasons.
    NO_TOKEN = "NO_TOKEN"  # noqa: S105
    MALFORMED_TOKEN = "MALFORMED_TOKEN"  # noqa: S105
    UNSUPPORTED_ALGORITHM = "UNSUPPORTED_ALGORITHM"
    UNRESOLVED_SIGNING_KEY = "UNRESOLVED_SIGNING_KEY"
    UNTRUSTED_SIGNATURE = "UNTRUSTED_SIGNATURE"
    EXPIRED = "EXPIRED"
    NOT_YET_VALID = "NOT_YET_VALID"
    WRONG_AUDIENCE = "WRONG_AUDIENCE"
    WRONG_ISSUER = "WRONG_ISSUER"
    MISSING_CLAIM = "MISSING_CLAIM"
    UNEXPECTED_WORKLOAD = "UNEXPECTED_WORKLOAD"
    UNEXPECTED_IMAGE_DIGEST = "UNEXPECTED_IMAGE_DIGEST"
    UNEXPECTED_HARDWARE = "UNEXPECTED_HARDWARE"
    DEBUG_MODE_ENABLED = "DEBUG_MODE_ENABLED"
    #: The fail-closed backstop: something raised that we did not anticipate.
    #: Still unattested, still recorded, never an upgrade.
    VERIFICATION_ERROR = "VERIFICATION_ERROR"


class AttestationPolicyError(ValueError):
    """The policy itself is unusable.

    Raised at construction, not at verification. A policy with no expected image
    digest would verify a token from any image, so an under-specified policy is a
    programming error and fails loudly rather than silently permitting more.
    """


@dataclass(frozen=True, kw_only=True)
class AttestationPolicy:
    """Exactly what a token must say before it counts as verified."""

    #: The nonce/audience this token was requested with. Rejects replay of a
    #: token minted for a different consumer.
    audience: str
    #: `sha256:<64 hex>` of the workload image we expect to be running. Accepted
    #: here in any case and with surrounding whitespace, because a developer
    #: writes this value; it is normalised once, at comparison time. The *token*
    #: side is never normalised — see `_evidence_from_claims`.
    image_digest: str
    #: The workload's service account — the workload identity half of the check.
    workload_service_account: str
    issuer: str = CONFIDENTIAL_SPACE_ISSUER
    allowed_hardware_models: frozenset[str] = DEFAULT_ALLOWED_HARDWARE_MODELS
    required_software_name: str = "CONFIDENTIAL_SPACE"
    #: Confidential Space reports `disabled-since-boot` when no debugger has been
    #: attached since boot. Anything else means the memory encryption guarantee
    #: does not hold and the token must not be honoured.
    required_debug_status: str = "disabled-since-boot"
    #: Bounded tolerance for clock drift on `exp`, `iat` and `nbf`.
    max_clock_skew_seconds: int = 60

    def __post_init__(self) -> None:
        if not self.audience.strip():
            raise AttestationPolicyError("Policy has no audience")
        if not self.issuer.strip():
            raise AttestationPolicyError("Policy has no issuer")
        if not self.workload_service_account.strip():
            raise AttestationPolicyError("Policy has no expected workload service account")
        if not _DIGEST_SHAPE.match(self.image_digest.strip().lower()):
            raise AttestationPolicyError(
                f"Policy image digest is not a sha256 digest: {self.image_digest!r}"
            )
        if not self.allowed_hardware_models:
            raise AttestationPolicyError("Policy allows no hardware model")
        if not self.required_software_name.strip():
            raise AttestationPolicyError("Policy has no required software name")
        if not self.required_debug_status.strip():
            raise AttestationPolicyError("Policy has no required debug status")
        if self.max_clock_skew_seconds < 0:
            raise AttestationPolicyError("Policy clock skew must not be negative")


@dataclass(frozen=True, kw_only=True)
class AttestationEvidence:
    """The verified facts, and only facts that were verified.

    Constructed in exactly one place — `_evidence_from_claims`, after every check
    has passed — and `test_evidence_is_constructed_in_exactly_one_place` sweeps
    the backend to keep it that way. Holding one is what it means for the trust
    level to be attested, so it carries the same single-producer rule as the enum
    member itself. The type is an ordinary dataclass, so that rule is a property
    of the codebase enforced by a test, not something Python prevents.
    """

    issuer: str
    audience: str
    subject: str
    image_digest: str
    image_reference: str
    workload_service_account: str
    hardware_model: str
    software_name: str
    debug_status: str
    issued_at: datetime
    expires_at: datetime
    verified_at: datetime


@dataclass(frozen=True, kw_only=True)
class AttestationOutcome:
    """The result of a verification attempt. Never ambiguous.

    Invariant, enforced in `__post_init__` and pinned by
    `test_an_outcome_cannot_be_upgraded_without_evidence`: without evidence the
    trust level is `DEVELOPMENT_ISOLATION`. Since evidence is constructed in one
    place, after every check, the only upgraded outcome that can exist is the one
    `verify_attestation_token` returns.
    """

    trust_level: TrustLevel
    evidence: AttestationEvidence | None = None
    failure: AttestationFailure | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.evidence is None and self.trust_level is not TrustLevel.DEVELOPMENT_ISOLATION:
            raise ValueError("An outcome without verified evidence cannot raise the trust level")
        if self.evidence is not None and self.failure is not None:
            raise ValueError("An outcome cannot both hold evidence and record a failure")
        if self.evidence is None and self.failure is None:
            raise ValueError("An outcome without evidence must record why")

    @property
    def verified(self) -> bool:
        return self.evidence is not None

    @classmethod
    def rejected(cls, failure: AttestationFailure, detail: str) -> AttestationOutcome:
        """Every failure path lands here, and every one of them is unattested."""
        return cls(
            trust_level=TrustLevel.DEVELOPMENT_ISOLATION,
            evidence=None,
            failure=failure,
            detail=detail,
        )


@runtime_checkable
class AttestationKeyResolver(Protocol):
    """Supplies the public key a token claims to be signed with.

    Separated from verification so the key source — a JWKS endpoint, a pinned
    certificate — can change without touching a single check.
    """

    def signing_key_for(self, token: str) -> Any: ...


class JwksAttestationKeyResolver:
    """Resolves a signing key from a JWKS endpoint via PyJWKClient.

    The URL has no default on purpose: naming an endpoint we have not verified
    would be a guess embedded in a security control. `F8-02` supplies the real
    Confidential Space JWKS URL when that infrastructure exists.

    `signing_key_for` performs **blocking** network I/O on a cache miss. Async
    callers must run it on a worker thread.
    """

    def __init__(self, jwks_url: str, *, client: Any | None = None) -> None:
        if client is None and not jwks_url.strip():
            raise ValueError("A JWKS URL is required")
        self._client = client if client is not None else jwt.PyJWKClient(jwks_url, cache_keys=True)

    def signing_key_for(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


@runtime_checkable
class AttestationVerifier(Protocol):
    """The verification interface. Implementations never widen what counts."""

    def verify(self, token: str) -> AttestationOutcome: ...


@dataclass(frozen=True)
class ConfidentialSpaceAttestationVerifier:
    """Binds a policy to a key source. Verifies; never fetches.

    This class holds no token-acquisition path — it cannot produce an outcome
    without being handed a token by a caller that obtained one for real — and
    nothing in the backend calls it yet, asserted by
    `test_no_backend_module_obtains_an_attestation_token_yet`.
    """

    policy: AttestationPolicy
    key_resolver: AttestationKeyResolver
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    def verify(self, token: str) -> AttestationOutcome:
        return verify_attestation_token(
            token,
            policy=self.policy,
            key_resolver=self.key_resolver,
            clock=self.clock,
        )


class _RejectedError(Exception):
    """Internal control flow. Every raise site becomes an unattested outcome."""

    def __init__(self, failure: AttestationFailure, detail: str) -> None:
        super().__init__(detail)
        self.failure = failure
        self.detail = detail


def verify_attestation_token(
    token: str,
    *,
    policy: AttestationPolicy,
    key_resolver: AttestationKeyResolver,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AttestationOutcome:
    """The only function in MCPForge that may raise the trust level.

    Checked, in this order: a non-empty token; an accepted signing algorithm,
    refused before the key is resolved; a resolvable and parseable signing key;
    the signature; issuer; audience; expiry, not-before and issued-at within a
    bounded clock skew; the presence of every claim in `REQUIRED_CLAIMS`; then,
    on the verified claims — subject, hardware model, software stack, debug
    status, workload service account, a single audience that is exactly ours,
    and the container image digest. Any failure returns `DEVELOPMENT_ISOLATION`
    with a reason, and this function raises nothing. There is no path through it
    that returns an attested outcome without every check above having passed.

    Do not add a second producer. `tests/test_attestation.py` walks the AST of
    every backend module and fails if one appears. That check matches on names,
    so — like the approval sweep in `02_ARCHITECTURE.md` §6 — it catches
    straightforwardly-written code and is not claimed to defeat deliberate
    indirection such as `getattr(TrustLevel, name)`.
    """
    try:
        claims = _verified_claims(token, policy=policy, key_resolver=key_resolver)
        evidence = _evidence_from_claims(claims, policy=policy, verified_at=clock())
    except _RejectedError as rejection:
        return AttestationOutcome.rejected(rejection.failure, rejection.detail)
    except Exception as exc:
        # Fail-closed backstop for a failure mode we did not anticipate. Every
        # *known* failure is raised as a `_RejectedError` above and arrives with
        # a named reason; reaching this line means our code is wrong, not that
        # the token is merely bad — so the tests treat arriving here as a defect
        # rather than as a pass. Four escapes reached it during review (see the
        # module docstring); each was closed by name, and the claim-shape matrix
        # in `tests/test_attestation.py` asserts that nothing it can construct
        # gets here. That is a bound on what is demonstrated, not a proof that
        # the line is unreachable — which is why it stays.
        return AttestationOutcome.rejected(
            AttestationFailure.VERIFICATION_ERROR,
            f"Attestation verification failed unexpectedly: {type(exc).__name__}: {exc}",
        )

    return AttestationOutcome(trust_level=TrustLevel.HARDWARE_ATTESTED, evidence=evidence)


def _verified_claims(
    token: str,
    *,
    policy: AttestationPolicy,
    key_resolver: AttestationKeyResolver,
) -> dict[str, Any]:
    """Cryptographic half: the token is authentic, current and addressed to us."""
    if not token or not token.strip():
        raise _RejectedError(AttestationFailure.NO_TOKEN, "No attestation token was supplied")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        # Broader than the `InvalidTokenError` PyJWT documents here, for the same
        # reason as the key handler below: the siblings under `PyJWTError` are
        # not subclasses. No reachable case exercises the extra breadth today, so
        # this mutation survives; narrowing it is still the wrong direction.
        raise _RejectedError(
            AttestationFailure.MALFORMED_TOKEN, f"Unreadable token header: {exc}"
        ) from exc

    algorithm = header.get("alg")
    if algorithm not in ACCEPTED_ALGORITHMS:
        raise _RejectedError(
            AttestationFailure.UNSUPPORTED_ALGORITHM,
            f"Token algorithm {algorithm!r} is not accepted",
        )

    try:
        key = key_resolver.signing_key_for(token)
    except Exception as exc:  # key resolvers raise several unrelated types
        raise _RejectedError(
            AttestationFailure.UNRESOLVED_SIGNING_KEY, f"Could not resolve signing key: {exc}"
        ) from exc

    # Parse the key here rather than letting `jwt.decode` do it, so that a key
    # problem is attributable to the key. PyJWT raises `InvalidKeyError` for an
    # unparseable key and a bare `TypeError` for an object that is not a key at
    # all; catching those around `decode` would also swallow a `TypeError` from
    # claim handling and mislabel it `UNRESOLVED_SIGNING_KEY`.
    try:
        prepared_key = jwt.get_algorithm_by_name(algorithm).prepare_key(key)
    except Exception as exc:
        raise _RejectedError(
            AttestationFailure.UNRESOLVED_SIGNING_KEY,
            f"The resolved signing key is unusable: {exc}",
        ) from exc

    # A *private* key is the likeliest real misconfiguration of a key source, and
    # `prepare_key` accepts one happily. PyJWT then calls `key.verify(...)`, which
    # a private key does not have, and the `AttributeError` used to reach the
    # fail-closed backstop — found by the reviewer on round 3. Every asymmetric
    # verification key exposes `verify`; a signing key does not.
    if not hasattr(prepared_key, "verify"):
        raise _RejectedError(
            AttestationFailure.UNRESOLVED_SIGNING_KEY,
            f"The resolved signing key is not a verification key: {type(prepared_key).__name__}",
        )

    try:
        decoded: dict[str, Any] = jwt.decode(
            token,
            prepared_key,
            algorithms=list(ACCEPTED_ALGORITHMS),
            audience=policy.audience,
            issuer=policy.issuer,
            leeway=policy.max_clock_skew_seconds,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.ExpiredSignatureError as exc:
        raise _RejectedError(AttestationFailure.EXPIRED, "Attestation token has expired") from exc
    except jwt.ImmatureSignatureError as exc:
        raise _RejectedError(
            AttestationFailure.NOT_YET_VALID, "Attestation token is not yet valid"
        ) from exc
    except jwt.InvalidAudienceError as exc:
        raise _RejectedError(
            AttestationFailure.WRONG_AUDIENCE, "Token audience does not match the expected audience"
        ) from exc
    except jwt.InvalidIssuerError as exc:
        raise _RejectedError(
            AttestationFailure.WRONG_ISSUER, "Token issuer is not the expected attestation service"
        ) from exc
    except jwt.MissingRequiredClaimError as exc:
        raise _RejectedError(
            AttestationFailure.MISSING_CLAIM, f"Token is incomplete: {exc}"
        ) from exc
    except jwt.InvalidSignatureError as exc:
        raise _RejectedError(
            AttestationFailure.UNTRUSTED_SIGNATURE, "Token signature did not verify"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise _RejectedError(AttestationFailure.MALFORMED_TOKEN, f"Invalid token: {exc}") from exc
    except (ArithmeticError, TypeError) as exc:
        # PyJWT converts `exp`, `nbf` and `iat` with `int()` *inside* its own
        # validation, and neither failure mode of `int()` is a `PyJWTError`:
        # `int(inf)` raises `OverflowError` (an `ArithmeticError`) and
        # `int(None)`, `int([])`, `int({})` raise `TypeError`. Both escaped every
        # handler above and reached the fail-closed backstop — the `inf` case
        # found on round 2, the `nbf` case on round 3, because `nbf` is validated
        # by PyJWT but is not in `REQUIRED_CLAIMS` and had no adversarial shape.
        # A timestamp we cannot convert is a claim we cannot check.
        raise _RejectedError(
            AttestationFailure.MISSING_CLAIM,
            f"A token timestamp claim is not a usable number: {type(exc).__name__}: {exc}",
        ) from exc
    except jwt.PyJWTError as exc:
        # The siblings of `InvalidTokenError` under `PyJWTError` — `InvalidKeyError`
        # and the PyJWK* errors — are not subclasses, so the handler above does
        # not catch them; this does. Every key problem we can actually reach is
        # attributed at `prepare_key` above and never gets here, so no test
        # exercises this line and its mutation survives. It is kept because a
        # `PyJWTError` we did not enumerate must still mean "unverified", and a
        # dedicated `UNRESOLVED_SIGNING_KEY` branch here would be unreachable
        # code claiming to do work — which is what round 2 of this review found.
        raise _RejectedError(AttestationFailure.MALFORMED_TOKEN, f"Invalid token: {exc}") from exc

    return decoded


def _evidence_from_claims(
    claims: dict[str, Any],
    *,
    policy: AttestationPolicy,
    verified_at: datetime,
) -> AttestationEvidence:
    """Semantic half: the authentic token describes the workload we expect."""
    subject = _required_str(claims, "sub")
    hardware_model = _required_str(claims, "hwmodel")
    software_name = _required_str(claims, "swname")
    debug_status = _required_str(claims, "dbgstat")
    service_accounts = _required_service_accounts(claims)

    container = claims.get("submods", {})
    container = container.get("container") if isinstance(container, dict) else None
    if not isinstance(container, dict):
        raise _RejectedError(
            AttestationFailure.MISSING_CLAIM, "Token carries no submods.container claim"
        )
    image_digest = _required_str(container, "image_digest")
    image_reference = _required_str(container, "image_reference")
    audience = _single_audience(claims)

    if hardware_model not in policy.allowed_hardware_models:
        raise _RejectedError(
            AttestationFailure.UNEXPECTED_HARDWARE,
            f"Hardware model {hardware_model!r} is not an allowed confidential platform",
        )
    if software_name != policy.required_software_name:
        raise _RejectedError(
            AttestationFailure.UNEXPECTED_HARDWARE,
            f"Software stack {software_name!r} is not {policy.required_software_name}",
        )
    if debug_status != policy.required_debug_status:
        raise _RejectedError(
            AttestationFailure.DEBUG_MODE_ENABLED,
            f"Debug status {debug_status!r} is not {policy.required_debug_status}",
        )
    # Membership, not equality. The claim is `google_service_accounts` — plural,
    # and an array of strings, per Google's Confidential Space token-claims
    # reference. An earlier version read a singular `google_service_account`
    # string, which no real token carries: the verifier would have rejected
    # every genuine token for a missing required claim, and the F8-02b attribute
    # condition mirrored the same mistake. Both were self-consistent with their
    # own fixtures, which is why the tests passed.
    if policy.workload_service_account not in service_accounts:
        raise _RejectedError(
            AttestationFailure.UNEXPECTED_WORKLOAD,
            "Token workload service accounts do not include the expected workload identity",
        )
    # Exact match against a canonical `sha256:<64 lowercase hex>`. The policy side
    # is normalised because a developer writes it; the token side is not
    # normalised at all — not case-folded, not stripped — because a digest that is
    # not in canonical form is not a digest we set an expectation for. No prefix
    # match, no "starts with", no case-insensitive comparison.
    #
    # The shape check is redundant with the equality below (a non-canonical digest
    # cannot equal a canonical one) and is kept because it names the invariant the
    # equality relies on and gives a distinct message. Its mutation survives; the
    # equality's does not.
    if not _DIGEST_SHAPE.match(image_digest):
        raise _RejectedError(
            AttestationFailure.UNEXPECTED_IMAGE_DIGEST,
            "Running image digest is not a canonical sha256 digest",
        )
    if image_digest != policy.image_digest.strip().lower():
        raise _RejectedError(
            AttestationFailure.UNEXPECTED_IMAGE_DIGEST,
            "Running image digest is not the expected image digest",
        )

    return AttestationEvidence(
        issuer=_required_str(claims, "iss"),
        audience=audience,
        subject=subject,
        image_digest=image_digest,
        image_reference=image_reference,
        workload_service_account=policy.workload_service_account,
        hardware_model=hardware_model,
        software_name=software_name,
        debug_status=debug_status,
        issued_at=_required_timestamp(claims, "iat"),
        expires_at=_required_timestamp(claims, "exp"),
        verified_at=verified_at,
    )


def _single_audience(claims: dict[str, Any]) -> str:
    """The audience is a per-run nonce, so exactly one is expected.

    `jwt.decode` has already established that our audience is *among* the token's
    audiences; that is correct per RFC 7519 and not enough here. A token
    addressed to us and to somebody else is a token somebody else also holds, so
    this narrows "contains ours" to "is exactly ours". It does not re-check the
    value — PyJWT did — it checks that there is only one.
    """
    audience = claims.get("aud")
    if isinstance(audience, list):
        if len(audience) != 1:
            raise _RejectedError(
                AttestationFailure.WRONG_AUDIENCE,
                "Token is addressed to more than one audience",
            )
        audience = audience[0]
    # UNREACHABLE TODAY, kept deliberately. `jwt.decode` has already matched the
    # `aud` claim against `policy.audience`, which is a non-empty string, so a
    # non-string cannot get this far and this branch has no test that kills its
    # mutation. It stays because it is the type contract of the return value —
    # the caller stores it as evidence — and because it would be the only thing
    # standing here if the decode-side audience check were ever relaxed. Stated
    # rather than dressed up as work: an unreachable guard is only harmful when a
    # comment claims it is doing something, which is round 2's finding.
    if not isinstance(audience, str) or not audience:
        raise _RejectedError(
            AttestationFailure.WRONG_AUDIENCE, "Token audience claim is missing or not a string"
        )
    return audience


def _required_service_accounts(claims: dict[str, Any]) -> tuple[str, ...]:
    """The `google_service_accounts` claim: a non-empty array of non-empty strings.

    Plural and an array, per Google's Confidential Space token-claims reference —
    "The validated service accounts that are running the Confidential Space
    workload." Values are returned exactly as they arrived, for the same reason
    `_required_str` strips nothing.

    Anything else is a missing claim, so a token carrying a bare string, an empty
    array, or a list with a non-string element is rejected rather than coerced.
    `test_the_service_account_claim_must_be_an_array_of_strings` fails if that
    stops holding.
    """
    value = claims.get("google_service_accounts")
    if not isinstance(value, list) or not value:
        raise _RejectedError(
            AttestationFailure.MISSING_CLAIM,
            "Token carries no google_service_accounts array",
        )
    accounts = tuple(item for item in value if isinstance(item, str) and item.strip())
    if len(accounts) != len(value):
        raise _RejectedError(
            AttestationFailure.MISSING_CLAIM,
            "google_service_accounts contains a value that is not a non-empty string",
        )
    return accounts


def _required_str(claims: dict[str, Any], name: str) -> str:
    """A non-empty string claim, returned **exactly as it arrived**.

    Nothing is stripped. Every value this returns is either compared for exact
    equality against policy or recorded as evidence, and a control that silently
    normalises its input does not mean what its documentation says: round 2 of
    this ticket's review found `"  sha256:<hex>  "` and `" GCP_AMD_SEV "`
    verifying while both the code comment and `03_SECURITY_ACCESS.md` claimed an
    exact match. A whitespace-only value is treated as missing, because it
    carries no claim at all.
    """
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _RejectedError(
            AttestationFailure.MISSING_CLAIM, f"Token claim {name!r} is missing or not a string"
        )
    return value


def _required_timestamp(claims: dict[str, Any], name: str) -> datetime:
    """A timestamp claim, converted. Only called for `iat` and `exp`.

    There is no finiteness guard here, and the type guard below is unreachable:
    both claims are in `REQUIRED_CLAIMS`, so `jwt.decode` has already required
    them and converted both with `int()`, and every value that conversion rejects
    is turned into a named failure in `_verified_claims`. Round 2 of this
    ticket's review found a finiteness guard here that never ran, under a comment
    saying it was the thing doing the work. The distinction being drawn is not
    "unreachable code is forbidden" but "a comment must not claim work the line
    does not do".
    """
    value = claims.get(name)
    # UNREACHABLE TODAY: see the docstring. Kept as the type contract of the
    # conversion below, which would raise `TypeError` — a type this function does
    # not catch — on a string or `None`. Its mutation survives, by construction.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _RejectedError(
            AttestationFailure.MISSING_CLAIM, f"Token claim {name!r} is missing or not a timestamp"
        )
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, ValueError, OSError) as exc:
        # A signed token may still carry `exp: 1e30`. Converting it raises
        # `OverflowError: timestamp out of range for platform time_t`, which
        # would leave this function as an exception rather than an outcome.
        raise _RejectedError(
            AttestationFailure.MISSING_CLAIM,
            f"Token claim {name!r} is not a representable timestamp: {exc}",
        ) from exc
