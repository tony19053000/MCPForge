"""Health and capability reporting.

Reports what is actually configured. An unconfigured integration says so; it is
never reported as ready, and never stubbed to look ready.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from mcpforge.config import Settings

router = APIRouter(tags=["health"])


class CapabilityState(BaseModel):
    authentication: bool
    gemini: bool


class HealthResponse(BaseModel):
    status: str
    environment: str
    version: str
    secure_execution: str
    hardware_attested: bool
    configured: CapabilityState


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        environment=settings.mcpforge_env.value,
        version=request.app.version,
        # Phase 1 has no executor yet. This reports the configured intent, and
        # hardware_attested is False until real attestation exists (Phase 8).
        secure_execution=settings.secure_executor.value,
        hardware_attested=False,
        configured=CapabilityState(
            authentication=settings.auth_configured,
            gemini=settings.gemini_configured,
        ),
    )
