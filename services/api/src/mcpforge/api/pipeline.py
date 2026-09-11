"""The pipeline, driven by the developer — F9-01.

One route per stage of `orchestration/pipeline.py`. Every route here is a
**human** action: it arrives with a verified token, its transitions are
recorded with `Origin.HUMAN` and the caller's subject as actor, and it can only
move the run through a gate by naming an approval the developer already decided
in `api/approvals.py`. Nothing here decides an approval, and nothing reads an
agent's text.

When a stage reaches a gate, the route opens it through `open_gate_request` —
the one approval-creation path the agent surface also uses — bound to the hash
of the artifact the stage just stored.

The agent surface (`api/agent.py`) is unchanged by this router. Its stage
endpoints still record a request and start nothing, because
`02_ARCHITECTURE.md` §10.1 holds that no agent endpoint moves the run.
"""

from __future__ import annotations

from collections.abc import Awaitable

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from mcpforge.api.approvals import ApprovalResponse, open_gate_request
from mcpforge.api.deps import CurrentIdentity
from mcpforge.models.core import Origin, RunEvent, RunState
from mcpforge.models.transitions import ApprovalRequiredError, IllegalTransitionError
from mcpforge.orchestration.pipeline import (
    Actor,
    ApprovalNotCurrentError,
    Pipeline,
    PipelineError,
    PipelineUnavailableError,
    PullRequestRefusedError,
    StepResult,
)
from mcpforge.store.port import NotFoundError, Store

router = APIRouter(prefix="/api/sessions/{session_id}/pipeline", tags=["pipeline"])

#: Every transition this router causes is a human's. A module constant, never
#: read from the request.
ORIGIN = Origin.HUMAN


class PipelineStepResponse(BaseModel):
    session_id: str
    state: RunState
    detail: str
    #: The gate this step opened, if it reached one. Always PENDING.
    approval: ApprovalResponse | None = None
    pull_request_url: str | None = None


class ConnectBody(BaseModel):
    #: Consume an agent's approved REPOSITORY_BINDING request.
    repository_binding_approval_id: str | None = Field(default=None, min_length=1, max_length=64)


class WorkflowsBody(BaseModel):
    #: Exactly one: the developer's own selection, or an approved agent request.
    workflow_ids: list[str] | None = Field(default=None, min_length=1, max_length=50)
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)


class DecisionBody(BaseModel):
    approval_id: str = Field(min_length=1, max_length=64)


class RetryableDecisionBody(BaseModel):
    #: Required when leaving a gate; absent when retrying a running step.
    approval_id: str | None = Field(default=None, min_length=1, max_length=64)


def _pipeline(request: Request) -> Pipeline:
    state = request.app.state
    return Pipeline(
        store=state.store,
        gemini=state.gemini,
        executor=state.executor,
        github=state.github,
        options=state.pipeline_options,
    )


def _actor(identity: CurrentIdentity) -> Actor:
    return Actor(name=f"user:{identity.subject}", origin=ORIGIN)


async def _respond(request: Request, step: Awaitable[StepResult]) -> PipelineStepResponse:
    try:
        result = await step
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc
    except (ApprovalRequiredError, ApprovalNotCurrentError, PullRequestRefusedError) as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except PipelineUnavailableError as exc:
        detail = str(exc)
        # Why no executor is attached, when the factory in `main.py` declined to
        # build one (T1). Stated, not hidden behind a bare 503.
        reason = getattr(request.app.state, "executor_unavailable_reason", None)
        if reason and request.app.state.executor is None:
            detail = f"{detail} Secure executor not attached: {reason}"
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail) from exc
    except (IllegalTransitionError, PipelineError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    approval: ApprovalResponse | None = None
    if result.gate_request is not None:
        store: Store = request.app.state.store
        opened = await open_gate_request(
            store,
            result.session,
            gate=result.gate_request.gate,
            artifact_hash=result.gate_request.artifact_hash,
            summary=result.gate_request.summary,
        )
        await store.append_event(
            RunEvent(
                session_id=result.session.id,
                kind="approval.requested",
                label=f"Awaiting your decision: {result.gate_request.gate.value}",
                detail={"approval_id": opened.id},
                origin=Origin.SYSTEM,
            )
        )
        approval = ApprovalResponse.of(opened)

    return PipelineStepResponse(
        session_id=result.session.id,
        state=result.session.state,
        detail=result.detail,
        approval=approval,
        pull_request_url=result.pull_request_url,
    )


@router.post("/connect", response_model=PipelineStepResponse)
async def connect(
    session_id: str,
    identity: CurrentIdentity,
    request: Request,
    body: ConnectBody | None = None,
) -> PipelineStepResponse:
    return await _respond(
        request,
        _pipeline(request).connect(
            session_id,
            identity.subject,
            _actor(identity),
            repository_binding_approval_id=body.repository_binding_approval_id if body else None,
        ),
    )


@router.post("/analysis", response_model=PipelineStepResponse)
async def analyze(
    session_id: str, identity: CurrentIdentity, request: Request
) -> PipelineStepResponse:
    return await _respond(
        request, _pipeline(request).analyze(session_id, identity.subject, _actor(identity))
    )


@router.post("/workflows", response_model=PipelineStepResponse)
async def select_workflows(
    session_id: str, body: WorkflowsBody, identity: CurrentIdentity, request: Request
) -> PipelineStepResponse:
    return await _respond(
        request,
        _pipeline(request).select_workflows(
            session_id,
            identity.subject,
            _actor(identity),
            workflow_ids=body.workflow_ids,
            approval_id=body.approval_id,
        ),
    )


@router.post("/plan", response_model=PipelineStepResponse)
async def plan(
    session_id: str, identity: CurrentIdentity, request: Request
) -> PipelineStepResponse:
    return await _respond(
        request, _pipeline(request).plan(session_id, identity.subject, _actor(identity))
    )


@router.post("/patch", response_model=PipelineStepResponse)
async def generate(
    session_id: str,
    identity: CurrentIdentity,
    request: Request,
    body: RetryableDecisionBody | None = None,
) -> PipelineStepResponse:
    return await _respond(
        request,
        _pipeline(request).generate(
            session_id,
            identity.subject,
            _actor(identity),
            approval_id=body.approval_id if body else None,
        ),
    )


@router.post("/security-review", response_model=PipelineStepResponse)
async def review(
    session_id: str, identity: CurrentIdentity, request: Request
) -> PipelineStepResponse:
    return await _respond(
        request, _pipeline(request).review(session_id, identity.subject, _actor(identity))
    )


@router.post("/validation", response_model=PipelineStepResponse)
async def validate(
    session_id: str, body: DecisionBody, identity: CurrentIdentity, request: Request
) -> PipelineStepResponse:
    return await _respond(
        request,
        _pipeline(request).validate(
            session_id, identity.subject, _actor(identity), approval_id=body.approval_id
        ),
    )


@router.post("/pull-request/request", response_model=PipelineStepResponse)
async def request_pull_request(
    session_id: str, identity: CurrentIdentity, request: Request
) -> PipelineStepResponse:
    return await _respond(
        request,
        _pipeline(request).request_pull_request(session_id, identity.subject, _actor(identity)),
    )


@router.post("/pull-request", response_model=PipelineStepResponse)
async def create_pull_request(
    session_id: str,
    identity: CurrentIdentity,
    request: Request,
    body: RetryableDecisionBody | None = None,
) -> PipelineStepResponse:
    return await _respond(
        request,
        _pipeline(request).create_pull_request(
            session_id,
            identity.subject,
            _actor(identity),
            approval_id=body.approval_id if body else None,
        ),
    )


@router.post("/reject", response_model=PipelineStepResponse)
async def reject(
    session_id: str, body: DecisionBody, identity: CurrentIdentity, request: Request
) -> PipelineStepResponse:
    return await _respond(
        request,
        _pipeline(request).reject(
            session_id, identity.subject, _actor(identity), approval_id=body.approval_id
        ),
    )
