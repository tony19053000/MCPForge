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

**Reads (T3).** The GET routes at the bottom return what a stage stored, for
its owner only. They call no transition and append no event. A stage that has
not run reads as `null` — never a default — and a failed stage reads back as
its failure. Stored analysis and the tool plan are already readable through
`GET /api/agent/sessions/{id}/workflows` and `/plan`, so they are not repeated.
"""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError

from mcpforge.agents.validator import ValidationReport
from mcpforge.api.approvals import ApprovalResponse, open_gate_request
from mcpforge.api.deps import CurrentIdentity

# `logging.py` ships inside the attested workload image, so it cannot gain a
# public name without changing the trusted digest; the private one is reused.
from mcpforge.logging import _redact_text as redact_text
from mcpforge.models.core import (
    ApprovalGate,
    Artifact,
    ArtifactKind,
    Origin,
    RunEvent,
    RunState,
    Session,
)
from mcpforge.models.security import Finding, GateVerdict
from mcpforge.models.transitions import ApprovalRequiredError, IllegalTransitionError
from mcpforge.orchestration.pipeline import (
    PENDING_GATES,
    Actor,
    ApprovalNotCurrentError,
    Pipeline,
    PipelineError,
    PipelineUnavailableError,
    PullRequestRefusedError,
    StepResult,
)
from mcpforge.orchestration.validation_run import outcome_of
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


# ---------------------------------------------------------------------------
# Reads — ticket T3. Owner-scoped, read-only: no transition, no event.
# ---------------------------------------------------------------------------

#: A stored artifact that no longer parses is reported, never papered over.
MALFORMED = "The stored {what} is not in the shape this route reads."


async def _owned_session(request: Request, session_id: str, identity: CurrentIdentity) -> Session:
    store: Store = request.app.state.store
    try:
        return await store.get_session(session_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc


async def _stored(request: Request, session: Session, kind: ArtifactKind) -> Artifact | None:
    store: Store = request.app.state.store
    return await store.get_artifact(session.id, kind, session.owner_uid)


def _after_last_change(events: list[RunEvent], to: RunState | None = None) -> list[RunEvent]:
    """Events after the last state change (to `to`, if given)."""
    last = -1
    for i, event in enumerate(events):
        if event.kind == "state.changed" and (to is None or event.detail.get("to") == to.value):
            last = i
    return events[last + 1 :] if last >= 0 or to is None else []


def _failure_of(events: list[RunEvent]) -> str | None:
    """The step that failed since the run last moved, from the timeline.

    A failed running step leaves the session where it was (its retry
    self-loop), so the state alone does not say it failed; the `step.failed`
    event the orchestrator recorded does.
    """
    failures = [e for e in _after_last_change(events) if e.kind == "step.failed"]
    if not failures:
        return None
    reason = failures[-1].detail.get("reason")
    text = f"{failures[-1].label}: {reason}" if isinstance(reason, str) else failures[-1].label
    return redact_text(text)


class PendingGateResponse(BaseModel):
    gate: ApprovalGate
    artifact_kind: ArtifactKind
    artifact_hash: str | None
    #: The approval opened for this arrival at the gate, over the artifact the
    #: run is showing. `None` when none was opened. May already be decided —
    #: the run moves only when a pipeline POST consumes the decision.
    approval: ApprovalResponse | None


class RunStateResponse(BaseModel):
    session_id: str
    project_id: str
    state: RunState
    updated_at: datetime
    pending_gate: PendingGateResponse | None
    #: The latest failure since the run last moved, if any.
    failure: str | None


@router.get("/state", response_model=RunStateResponse)
async def get_run_state(
    session_id: str, identity: CurrentIdentity, request: Request
) -> RunStateResponse:
    session = await _owned_session(request, session_id, identity)
    store: Store = request.app.state.store
    events = await store.list_events(session.id, identity.subject)

    pending: PendingGateResponse | None = None
    if session.state in PENDING_GATES:
        gate, kind = PENDING_GATES[session.state]
        artifact = await _stored(request, session, kind)
        found: ApprovalResponse | None = None
        if artifact is not None:
            requested = [
                e
                for e in _after_last_change(events, session.state)
                if e.kind == "approval.requested"
            ]
            for event in reversed(requested):
                approval_id = event.detail.get("approval_id")
                if not isinstance(approval_id, str):
                    continue
                try:
                    approval = await store.get_approval(approval_id, identity.subject)
                except NotFoundError:
                    continue
                if (
                    approval.session_id == session.id
                    and approval.gate is gate
                    and approval.artifact_hash == artifact.hash
                ):
                    found = ApprovalResponse.of(approval)
                    break
        pending = PendingGateResponse(
            gate=gate,
            artifact_kind=kind,
            artifact_hash=artifact.hash if artifact is not None else None,
            approval=found,
        )

    return RunStateResponse(
        session_id=session.id,
        project_id=session.project_id,
        state=session.state,
        updated_at=session.updated_at,
        pending_gate=pending,
        failure=_failure_of(events),
    )


class DiffFileResponse(BaseModel):
    """One file, in the shape `apps/web/src/components/diff/diff-view.tsx`'s
    `DiffFile` takes — including its camelCase `affectedTool`."""

    path: str
    kind: str
    rationale: str
    affected_tool: str | None = Field(default=None, serialization_alias="affectedTool")
    diff: str
    added: int
    removed: int


class PatchResponse(BaseModel):
    #: The hash the PATCH and PULL_REQUEST approvals bind to.
    artifact_hash: str
    summary: str
    base_commit: str | None
    total_added: int
    total_removed: int
    files: list[DiffFileResponse]


@router.get("/patch", response_model=PatchResponse | None)
async def get_patch(
    session_id: str, identity: CurrentIdentity, request: Request
) -> PatchResponse | None:
    await _owned_session(request, session_id, identity)
    try:
        read = await _pipeline(request).read_patch(session_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc
    except PipelineError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if read is None:
        return None
    patch, stored = read
    return PatchResponse(
        artifact_hash=stored.hash,
        summary=patch.summary,
        base_commit=patch.base_commit,
        total_added=patch.total_added,
        total_removed=patch.total_removed,
        files=[
            DiffFileResponse(
                path=f.path,
                kind=f.kind.value,
                rationale=f.rationale,
                affected_tool=f.affected_tool,
                diff=f.unified_diff(),
                added=f.added_lines,
                removed=f.removed_lines,
            )
            for f in patch.files
        ],
    )


class SecurityReviewResponse(BaseModel):
    #: False when the reviewer produced no report; the gate then cannot pass.
    completed: bool
    #: `GateVerdict.passed`, decided by `evaluate_gate` — never the model's own
    #: view, which is `agent_said_pass`.
    passed: bool
    reason: str
    agent_said_pass: bool | None
    overridden: bool
    findings: list[Finding]


@router.get("/security-review", response_model=SecurityReviewResponse | None)
async def get_security_review(
    session_id: str, identity: CurrentIdentity, request: Request
) -> SecurityReviewResponse | None:
    session = await _owned_session(request, session_id, identity)
    artifact = await _stored(request, session, ArtifactKind.SECURITY_REVIEW)
    if artifact is None:
        return None
    if artifact.payload.get("completed") is not True:
        reason = artifact.payload.get("reason")
        return SecurityReviewResponse(
            completed=False,
            passed=False,
            reason=redact_text(reason if isinstance(reason, str) else "No reason was recorded."),
            agent_said_pass=None,
            overridden=False,
            findings=[],
        )
    try:
        verdict = GateVerdict.model_validate(artifact.payload["verdict"])
    except (KeyError, ValidationError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, MALFORMED.format(what="security review")
        ) from exc
    return SecurityReviewResponse(
        completed=True,
        passed=verdict.passed,
        reason=verdict.reason,
        agent_said_pass=verdict.agent_said_pass,
        overridden=verdict.overridden,
        findings=verdict.findings,
    )


CheckStatus = Literal["passed", "failed", "skipped"]


class ValidationCheckResponse(BaseModel):
    check_id: str
    component: str | None
    description: str
    #: From the exit code (`CheckEvidence.passed`), or "skipped" for a check
    #: that could not run and produced no evidence.
    status: CheckStatus
    exit_code: int | None
    timed_out: bool | None
    duration_seconds: float | None
    stdout_excerpt: str
    stderr_excerpt: str
    skip_reason: str | None


class ScoreComponentResponse(BaseModel):
    component: str
    label: str
    weight: int
    points: int
    checks_passed: int
    checks_executed: int
    #: Why the component scored what it did.
    detail: str


class ReadinessResponse(BaseModel):
    total: int
    max_total: int
    components: list[ScoreComponentResponse]


class ValidationResponse(BaseModel):
    #: False when validation could not run at all; then nothing below is evidence.
    completed: bool
    #: The pipeline's own verdict: every executed check passed and no tool
    #: check went unexecuted (`outcome_of`).
    passed: bool
    validated: bool
    summary: str | None
    reason: str | None
    failed_check_ids: list[str]
    unexecuted_tool_checks: list[str]
    checks: list[ValidationCheckResponse]
    score: ReadinessResponse | None
    dependency_source: str | None
    dependency_detail: str | None


def _dependency_fields(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, dict):
        return None, None
    source, detail = dependencies.get("source"), dependencies.get("detail")
    return (
        source if isinstance(source, str) else None,
        redact_text(detail) if isinstance(detail, str) else None,
    )


@router.get("/validation", response_model=ValidationResponse | None)
async def get_validation(
    session_id: str, identity: CurrentIdentity, request: Request
) -> ValidationResponse | None:
    session = await _owned_session(request, session_id, identity)
    artifact = await _stored(request, session, ArtifactKind.VALIDATION)
    if artifact is None:
        return None
    payload = artifact.payload
    source, detail = _dependency_fields(payload)

    if payload.get("completed") is not True:
        reason = payload.get("reason")
        return ValidationResponse(
            completed=False,
            passed=False,
            validated=False,
            summary=None,
            reason=redact_text(reason) if isinstance(reason, str) else None,
            failed_check_ids=[],
            unexecuted_tool_checks=[],
            checks=[],
            score=None,
            dependency_source=source,
            dependency_detail=detail,
        )

    try:
        report = ValidationReport.model_validate(payload["report"])
    except (KeyError, ValidationError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, MALFORMED.format(what="validation report")
        ) from exc
    # The same function the pipeline used to decide, re-run over the same
    # stored report — so this can only agree with the transition it caused.
    outcome = outcome_of(report)

    checks = [
        ValidationCheckResponse(
            check_id=c.check_id,
            component=c.component.value if c.component is not None else None,
            description=c.description,
            status="passed" if c.passed else "failed",
            exit_code=c.evidence.exit_code,
            timed_out=c.evidence.timed_out,
            duration_seconds=c.evidence.duration_seconds,
            stdout_excerpt=redact_text(c.evidence.stdout_excerpt),
            stderr_excerpt=redact_text(c.evidence.stderr_excerpt),
            skip_reason=None,
        )
        for c in report.checks
    ] + [
        ValidationCheckResponse(
            check_id=s.check_id,
            component=s.component.value if s.component is not None else None,
            description=s.description,
            status="skipped",
            exit_code=None,
            timed_out=None,
            duration_seconds=None,
            stdout_excerpt="",
            stderr_excerpt="",
            skip_reason=s.reason,
        )
        for s in report.skipped
    ]
    return ValidationResponse(
        completed=True,
        passed=outcome.passed,
        validated=not outcome.unexecuted,
        summary=outcome.summary,
        reason=None,
        failed_check_ids=list(outcome.failed_check_ids),
        unexecuted_tool_checks=list(outcome.unexecuted),
        checks=checks,
        score=ReadinessResponse(
            total=report.score.total,
            max_total=report.score.max_total,
            components=[
                ScoreComponentResponse(
                    component=row.component.value,
                    label=row.label,
                    weight=row.weight,
                    points=row.points,
                    checks_passed=row.checks_passed,
                    checks_executed=row.checks_executed,
                    detail=row.detail,
                )
                for row in report.score.components
            ],
        ),
        dependency_source=source,
        dependency_detail=detail,
    )


PullRequestStatus = Literal["AWAITING_APPROVAL", "CREATING", "FAILED", "OPENED"]

#: Run states in which a pull request has been asked for.
_PR_ASKED = {RunState.PR_APPROVAL_PENDING, RunState.PR_APPROVED}


class PullRequestResponse(BaseModel):
    status: PullRequestStatus
    #: Set only from the recorded "pull request opened" event — never derived.
    url: str | None
    number: int | None
    branch: str | None
    failure: str | None


@router.get("/pull-request", response_model=PullRequestResponse | None)
async def get_pull_request(
    session_id: str, identity: CurrentIdentity, request: Request
) -> PullRequestResponse | None:
    """What the pull-request stage recorded. There is no PR artifact kind: the
    orchestrator records an opened pull request as an `artifact.ready` event
    carrying its url, number and branch, and a failed attempt as `step.failed`."""
    session = await _owned_session(request, session_id, identity)
    store: Store = request.app.state.store
    events = await store.list_events(session.id, identity.subject)

    opened = [e for e in events if e.kind == "artifact.ready" and "url" in e.detail]
    if opened:
        detail = opened[-1].detail
        url, number, branch = detail.get("url"), detail.get("number"), detail.get("branch")
        return PullRequestResponse(
            status="OPENED",
            url=url if isinstance(url, str) else None,
            number=number if isinstance(number, int) else None,
            branch=branch if isinstance(branch, str) else None,
            failure=None,
        )
    if session.state in _PR_ASKED:
        return PullRequestResponse(
            status="AWAITING_APPROVAL", url=None, number=None, branch=None, failure=None
        )
    if session.state is RunState.PR_CREATING:
        failure = _failure_of(events)
        return PullRequestResponse(
            status="FAILED" if failure else "CREATING",
            url=None,
            number=None,
            branch=None,
            failure=failure,
        )
    return None
