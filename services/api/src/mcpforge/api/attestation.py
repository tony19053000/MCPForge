"""Verify a Confidential Space attestation run — F8-02, the relying party's route.

`POST /api/attestation-runs/{run_id}/verify` makes this API process fetch the
run's delivered token and verify it, so the verification that raises the trust
panel's level is one this server performed. It needs a verified identity; it
takes no input but the run id — the audience, digest and service account all
come from the API's own record and configuration. Each run verifies once.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from mcpforge.api.deps import CurrentIdentity
from mcpforge.execution.attestation import TrustLevel
from mcpforge.execution.confidential_space import ConfidentialSpaceSecureExecutor

router = APIRouter(prefix="/api", tags=["attestation"])


class RunVerificationDto(BaseModel):
    run_id: str
    verified: bool
    failure: str | None
    attestation_failure: str | None
    detail: str | None
    trust_level: TrustLevel
    token_sha256_prefix: str | None
    consumed: bool


@router.post("/attestation-runs/{run_id}/verify", response_model=RunVerificationDto)
async def verify_attestation_run(
    run_id: str, identity: CurrentIdentity, request: Request
) -> RunVerificationDto:
    del identity
    executor = getattr(request.app.state, "executor", None)
    if not isinstance(executor, ConfidentialSpaceSecureExecutor):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This API process is not the Confidential Space relying party "
            "(SECURE_EXECUTOR is not confidential_space).",
        )
    result = await executor.verify_run(run_id)
    return RunVerificationDto(
        run_id=result.run_id,
        verified=result.outcome.verified,
        failure=result.failure.value if result.failure is not None else None,
        attestation_failure=(
            result.outcome.failure.value if result.outcome.failure is not None else None
        ),
        detail=result.outcome.detail,
        trust_level=executor.trust_level,
        token_sha256_prefix=result.token.sha256_prefix if result.token is not None else None,
        consumed=result.consumed,
    )
