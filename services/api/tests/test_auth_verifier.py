"""FirebaseIdTokenVerifier — F1-06 acceptance criteria."""

from __future__ import annotations

import time

import jwt
import pytest

from mcpforge.auth.firebase import FirebaseIdTokenVerifier
from mcpforge.auth.identity import AuthError, AuthNotConfiguredError, VerifiedIdentity
from tests.conftest import TEST_KID, TEST_PROJECT, MakeToken, StubJWKClient


@pytest.fixture
def verifier(jwks: StubJWKClient) -> FirebaseIdTokenVerifier:
    return FirebaseIdTokenVerifier(TEST_PROJECT, jwks_client=jwks)  # type: ignore[arg-type]


async def test_valid_token_yields_identity(
    verifier: FirebaseIdTokenVerifier, make_token: MakeToken
) -> None:
    identity = await verifier.verify(make_token())
    assert isinstance(identity, VerifiedIdentity)
    assert identity.subject == "user-123"
    assert identity.email == "dev@example.com"
    assert identity.email_verified is True
    assert identity.issuer == f"https://securetoken.google.com/{TEST_PROJECT}"


async def test_expired_token_rejected(
    verifier: FirebaseIdTokenVerifier, make_token: MakeToken
) -> None:
    with pytest.raises(AuthError, match="expired"):
        await verifier.verify(make_token(expires_in=-60))


async def test_wrong_audience_rejected(
    verifier: FirebaseIdTokenVerifier, make_token: MakeToken
) -> None:
    with pytest.raises(AuthError, match="audience"):
        await verifier.verify(make_token(audience="some-other-project"))


async def test_wrong_issuer_rejected(
    verifier: FirebaseIdTokenVerifier, make_token: MakeToken
) -> None:
    with pytest.raises(AuthError, match="issuer"):
        await verifier.verify(make_token(issuer="https://evil.example.com/"))


async def test_malformed_token_rejected(verifier: FirebaseIdTokenVerifier) -> None:
    with pytest.raises(AuthError):
        await verifier.verify("not-a-jwt")


async def test_empty_token_rejected(verifier: FirebaseIdTokenVerifier) -> None:
    with pytest.raises(AuthError, match="Empty"):
        await verifier.verify("   ")


async def test_unsigned_token_rejected(verifier: FirebaseIdTokenVerifier) -> None:
    """An 'alg: none' token must never be accepted."""
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "attacker",
            "aud": TEST_PROJECT,
            "iss": f"https://securetoken.google.com/{TEST_PROJECT}",
            "iat": now,
            "exp": now + 3600,
        },
        key="",
        algorithm="none",
        headers={"kid": TEST_KID},
    )
    with pytest.raises(AuthError):
        await verifier.verify(token)


async def test_token_signed_by_a_different_key_rejected(
    verifier: FirebaseIdTokenVerifier, make_token: MakeToken
) -> None:
    """Tampering with the payload invalidates the signature."""
    token = make_token()
    header, payload, _sig = token.split(".")
    forged = f"{header}.{payload}.{'A' * 342}"
    with pytest.raises(AuthError):
        await verifier.verify(forged)


async def test_missing_required_claim_rejected(
    verifier: FirebaseIdTokenVerifier, make_token: MakeToken
) -> None:
    with pytest.raises(AuthError):
        await verifier.verify(make_token(omit=("exp",)))


async def test_unconfigured_verifier_reports_unconfigured_not_invalid() -> None:
    """An unconfigured deployment must say so, not imply the token was bad."""
    verifier = FirebaseIdTokenVerifier(None)
    assert verifier.configured is False
    with pytest.raises(AuthNotConfiguredError):
        await verifier.verify("anything")
