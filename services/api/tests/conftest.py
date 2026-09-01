"""Test fixtures.

Authentication is tested against a locally generated RSA key pair rather than
mocked away, so the verifier's signature, issuer, audience and expiry checks are
all genuinely exercised.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, Protocol

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mcpforge.config import Environment, Settings

TEST_PROJECT = "mcpforge-test"
TEST_KID = "test-key-1"

# Every MCPFORGE-relevant variable, cleared before each test.
_ENV_VARS = (
    "MCPFORGE_ENV",
    "LOG_LEVEL",
    "API_CORS_ORIGINS",
    "GEMINI_BACKEND",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "FIREBASE_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_QUOTA_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "SECURE_EXECUTOR",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not depend on the developer's .env or shell.

    Without this, a real GEMINI_API_KEY in a local .env silently makes
    "unconfigured" tests pass for the wrong reason — or fail, which is how this
    was found. Settings is pinned to read no env file, and every relevant
    variable is cleared, so every test states its own configuration explicitly.
    """
    from mcpforge import config as config_module

    monkeypatch.setitem(config_module.Settings.model_config, "env_file", None)
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    config_module.get_settings.cache_clear()


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def private_pem(rsa_key: rsa.RSAPrivateKey) -> str:
    return rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


class MakeToken(Protocol):
    """Signature of the `make_token` fixture, so tests can be strictly typed."""

    def __call__(
        self,
        *,
        subject: str = ...,
        audience: str = ...,
        issuer: str | None = ...,
        expires_in: int = ...,
        email: str | None = ...,
        email_verified: bool = ...,
        omit: tuple[str, ...] = ...,
    ) -> str: ...


@pytest.fixture
def make_token(private_pem: str) -> MakeToken:
    def _make(
        *,
        subject: str = "user-123",
        audience: str = TEST_PROJECT,
        issuer: str | None = None,
        expires_in: int = 3600,
        email: str | None = "dev@example.com",
        email_verified: bool = True,
        omit: tuple[str, ...] = (),
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": subject,
            "aud": audience,
            "iss": issuer or f"https://securetoken.google.com/{TEST_PROJECT}",
            "iat": now,
            "exp": now + expires_in,
            "email": email,
            "email_verified": email_verified,
        }
        for key in omit:
            claims.pop(key, None)
        return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": TEST_KID})

    return _make


@pytest.fixture
def settings() -> Settings:
    return Settings(
        mcpforge_env=Environment.DEVELOPMENT,
        firebase_project_id=TEST_PROJECT,
        api_cors_origins="http://localhost:3000",
    )


@pytest.fixture
def unconfigured_settings() -> Settings:
    return Settings(mcpforge_env=Environment.DEVELOPMENT, firebase_project_id=None)


class StubJWKClient:
    """Serves the test key pair in place of Google's JWKS endpoint.

    Only the network fetch is substituted. Signature verification, issuer,
    audience and expiry checks all run for real against this key.
    """

    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        header = jwt.get_unverified_header(token)
        if header.get("kid") != TEST_KID:
            raise ValueError(f"Unknown kid: {header.get('kid')}")

        class _Key:
            key = self._key.public_key()

        return _Key()


@pytest.fixture
def jwks(rsa_key: rsa.RSAPrivateKey) -> Iterator[StubJWKClient]:
    yield StubJWKClient(rsa_key)
