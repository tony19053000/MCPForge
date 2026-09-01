"""Identity types and the TokenVerifier port — 02_ARCHITECTURE.md §3.2.

`VerifiedIdentity` is MCPForge's own type. No vendor identity object crosses
this boundary, so replacing the identity provider means writing one new
`TokenVerifier` and changing nothing else.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class VerifiedIdentity(BaseModel):
    """The only identity representation the rest of the backend ever sees."""

    model_config = {"frozen": True}

    subject: str = Field(min_length=1, description="Stable unique user id from the issuer")
    email: str | None = None
    email_verified: bool = False
    issuer: str = Field(min_length=1)
    claims: dict[str, Any] = Field(default_factory=dict)


class AuthError(Exception):
    """Token verification failed. Always surfaced as 401, never as a partial success."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AuthNotConfiguredError(AuthError):
    """Identity is not configured. Distinct from a rejected token so the product
    can report 'unconfigured' honestly instead of implying a bad credential."""


@runtime_checkable
class TokenVerifier(Protocol):
    """Verifies a bearer token and returns our identity type, or raises AuthError.

    Implementations must verify signature, issuer, audience and expiry. A verifier
    that trusts an unverified claim is a security defect, not an optimisation.
    """

    async def verify(self, raw_token: str) -> VerifiedIdentity: ...
