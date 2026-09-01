"""Approval endpoints — F2-05.

This is the mechanism every consequential step depends on, so the rules are
enforced here and not left to callers (03_SECURITY_ACCESS.md §7):

- A decision requires an authenticated user who owns the session.
- A decision binds to the artifact hash that was shown. If the artifact is
  regenerated, the approval no longer covers it.
- A decided approval cannot be decided again.
- Nothing a model says can create or decide an approval.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from mcpforge.api.deps import CurrentIdentity
from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    ApprovalStatus,
    Origin,
    RunEvent,
    utcnow,
)
from mcpforge.store.port import NotFoundError, Store

router = APIRouter(prefix="/api", tags=["approvals"])


class RequestApprovalBody(BaseModel):
    gate: ApprovalGate
    artifact_hash: str = Field(min_length=8, max_length=128)
    summary: str = Field(min_length=1, max_length=500)


class DecideBody(BaseModel):
    decision: ApprovalStatus

    def is_terminal(self) -> bool:
        return self.decision in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED)


class ApprovalResponse(BaseModel):
    id: str
    gate: ApprovalGate
    artifact_hash: str
    summary: str
    status: ApprovalStatus
    requested_at: datetime
    decided_at: datetime | None
    actor_uid: str | None

    @classmethod
    def of(cls, a: Approval) -> ApprovalResponse:
        return cls(
            id=a.id,
            gate=a.gate,
            artifact_hash=a.artifact_hash,
            summary=a.summary,
            status=a.status,
            requested_at=a.requested_at,
            decided_at=a.decided_at,
            actor_uid=a.actor_uid,
        )


def _store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


@router.post("/sessions/{session_id}/approvals", response_model=ApprovalResponse, status_code=201)
async def request_approval(
    session_id: str, body: RequestApprovalBody, identity: CurrentIdentity, request: Request
) -> ApprovalResponse:
    store = _store(request)
    try:
        session = await store.get_session(session_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc

    approval = await store.create_approval(
        Approval(
            project_id=session.project_id,
            session_id=session.id,
            gate=body.gate,
            artifact_hash=body.artifact_hash,
            summary=body.summary,
        )
    )
    await store.append_event(
        RunEvent(
            session_id=session.id,
            kind="approval.requested",
            label=f"Awaiting your decision: {body.gate.value}",
            detail={"approval_id": approval.id},
            origin=Origin.SYSTEM,
        )
    )
    return ApprovalResponse.of(approval)


@router.post("/approvals/{approval_id}/decide", response_model=ApprovalResponse)
async def decide_approval(
    approval_id: str, body: DecideBody, identity: CurrentIdentity, request: Request
) -> ApprovalResponse:
    store = _store(request)
    try:
        approval = await store.get_approval(approval_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found") from exc

    if not body.is_terminal():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "A decision must be APPROVED or REJECTED",
        )
    if approval.status is not ApprovalStatus.PENDING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This approval was already {approval.status.value.lower()}",
        )

    approval.status = body.decision
    approval.actor_uid = identity.subject  # from the verified token, never the body
    approval.decided_at = utcnow()
    await store.update_approval(approval, identity.subject)

    await store.append_event(
        RunEvent(
            session_id=approval.session_id,
            kind="approval.decided",
            label=f"{approval.gate.value}: {approval.status.value.lower()}",
            detail={"approval_id": approval.id},
            origin=Origin.HUMAN,
        )
    )
    return ApprovalResponse.of(approval)


@router.get("/approvals/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    approval_id: str, identity: CurrentIdentity, request: Request
) -> ApprovalResponse:
    try:
        approval = await _store(request).get_approval(approval_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approval not found") from exc
    return ApprovalResponse.of(approval)


class GateCheckResponse(BaseModel):
    """Whether a gate is actually open for a specific artifact.

    This is what the orchestrator will consult. It reads stored records only —
    never model output.
    """

    open: bool
    reason: str


@router.get("/sessions/{session_id}/gate", response_model=GateCheckResponse)
async def check_gate(
    session_id: str,
    gate: ApprovalGate,
    artifact_hash: str,
    identity: CurrentIdentity,
    request: Request,
) -> GateCheckResponse:
    store = _store(request)
    try:
        found = await store.find_approval(session_id, gate, artifact_hash, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc

    if found is None:
        return GateCheckResponse(
            open=False,
            reason="No approved decision exists for this gate and this exact artifact",
        )
    return GateCheckResponse(open=True, reason=f"Approved by {found.actor_uid}")


class EventResponse(BaseModel):
    id: str
    kind: str
    label: str
    detail: dict[str, object]
    origin: Origin
    created_at: datetime


@router.get("/sessions/{session_id}/events", response_model=list[EventResponse])
async def list_events(
    session_id: str, identity: CurrentIdentity, request: Request
) -> list[EventResponse]:
    try:
        events = await _store(request).list_events(session_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc
    return [
        EventResponse(
            id=e.id,
            kind=e.kind,
            label=e.label,
            detail=e.detail,
            origin=e.origin,
            created_at=e.created_at,
        )
        for e in events
    ]
