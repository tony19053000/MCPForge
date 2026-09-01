"""Firebase ID token verification, without the Firebase SDK and without credentials.

A Firebase ID token is a standard RS256 JWT signed by Google, with a public JWKS
endpoint. Verifying it needs only the project id — to derive the expected issuer
and audience — so this implementation depends on no service-account key. That
matters twice over: organization policy blocks key downloads, and the absence of
a vendor SDK keeps the eventual swap to direct Google OAuth to one adapter.

See 02_ARCHITECTURE.md §3.2 and 03_SECURITY_ACCESS.md §9.
"""

from __future__ import annotations

import jwt
from jwt import PyJWKClient

from mcpforge.auth.identity import (
    AuthError,
    AuthNotConfiguredError,
    VerifiedIdentity,
)

# Google's public JWKS for Firebase ID tokens. Public data; no credential involved.
FIREBASE_JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
)
FIREBASE_ISSUER_PREFIX = "https://securetoken.google.com/"


class FirebaseIdTokenVerifier:
    """Verifies signature, issuer, audience, expiry and subject. Nothing is assumed."""

    def __init__(
        self,
        project_id: str | None,
        *,
        jwks_client: PyJWKClient | None = None,
    ) -> None:
        self._project_id = project_id
        self._jwks_client = jwks_client
        if project_id and jwks_client is None:
            # PyJWKClient caches keys and refreshes on unknown kid.
            self._jwks_client = PyJWKClient(FIREBASE_JWKS_URL, cache_keys=True)

    @property
    def configured(self) -> bool:
        return bool(self._project_id) and self._jwks_client is not None

    async def verify(self, raw_token: str) -> VerifiedIdentity:
        if not self.configured:
            raise AuthNotConfiguredError("Identity is not configured: FIREBASE_PROJECT_ID is unset")
        assert self._jwks_client is not None  # narrowed by `configured`
        project_id = self._project_id
        assert project_id is not None

        if not raw_token or not raw_token.strip():
            raise AuthError("Empty bearer token")

        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(raw_token)
        except Exception as exc:  # PyJWKClient raises several unrelated types
            raise AuthError(f"Could not resolve signing key: {exc}") from exc

        try:
            claims = jwt.decode(
                raw_token,
                signing_key.key,
                algorithms=["RS256"],
                audience=project_id,
                issuer=f"{FIREBASE_ISSUER_PREFIX}{project_id}",
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("Token audience does not match this project") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("Token issuer does not match this project") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"Invalid token: {exc}") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise AuthError("Token has no usable subject")

        return VerifiedIdentity(
            subject=subject,
            email=claims.get("email"),
            email_verified=bool(claims.get("email_verified", False)),
            issuer=str(claims["iss"]),
            claims=claims,
        )
