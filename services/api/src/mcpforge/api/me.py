"""Identity echo endpoint — the smallest possible proof that auth is enforced."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from mcpforge.api.deps import CurrentIdentity

router = APIRouter(tags=["identity"])


class MeResponse(BaseModel):
    subject: str
    email: str | None
    email_verified: bool
    issuer: str


@router.get("/api/me", response_model=MeResponse)
async def me(identity: CurrentIdentity) -> MeResponse:
    # Only verified claims are echoed. Raw claims are never returned to the client.
    return MeResponse(
        subject=identity.subject,
        email=identity.email,
        email_verified=identity.email_verified,
        issuer=identity.issuer,
    )
