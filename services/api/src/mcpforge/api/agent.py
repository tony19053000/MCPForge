"""The agent surface — tickets F7-02, F7-03, F7-04.

Everything a WebMCP tool can reach lives under `/api/agent`. That is not a
convenience: it is how `Origin.AGENT` is established. Origin is derived from the
route the request arrived on, so it cannot be spoofed by a body field, a header
or a query parameter. An agent therefore cannot make its actions look like a
human's, which is the whole point of F7-04.

The security claim this router exists to prove (`01_PRD.md`, `CLAUDE.md` §6.4):

    An agent can drive MCPForge end to end, and still cannot approve anything.

Three properties make that true, and each is tested:

1. **No decide route here.** Deciding an approval lives in `api/approvals.py`
   and stamps `Origin.HUMAN` from a verified token. There is no agent-reachable
   path to it, and no second implementation of it. That is held by an AST sweep
   over every backend module rather than by enumerating this router's routes:
   only `api/approvals.py` may assign a decision field, copy one on with
   `model_copy(update=...)`, or call `create_approval` / `update_approval` /
   `decide_approval`. So a second router that simply *called* the real decision
   handler is caught too. The sweep is name-based and does not defeat deliberate
   indirection; what holds regardless of spelling is property 2's behavioural
   check against the stored record.
2. **Mutations stop at the gate.** Every mutation endpoint records an artifact,
   opens an approval request against it, and returns `awaiting_human_approval`.
   None of them transitions the session — the state is what the orchestrator
   reads, so moving it would be the gate opening with no decision. That is
   asserted behaviourally, by driving every endpoint here and comparing the
   stored session state before and after, not by reading this file's imports.
3. **Hashes are derived, not accepted.** The hash an approval binds to comes
   from the stored artifact's content. A caller cannot present one artifact and
   claim an approval bound to a different hash.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from mcpforge.api.approvals import open_gate_request
from mcpforge.api.deps import CurrentIdentity
from mcpforge.logging import get_logger
from mcpforge.models.core import (
    ApprovalGate,
    Artifact,
    ArtifactKind,
    Origin,
    Project,
    RunEvent,
    Session,
)
from mcpforge.store.port import NotFoundError, Store

log = get_logger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent"])

#: Every action taken through this router is an agent action. This constant is
#: the only place the origin comes from — it is never read from the request.
ORIGIN = Origin.AGENT


def _store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


async def _session(request: Request, session_id: str, identity: CurrentIdentity) -> Session:
    try:
        return await _store(request).get_session(session_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found") from exc


async def _project(request: Request, project_id: str, identity: CurrentIdentity) -> Project:
    try:
        return await _store(request).get_project(project_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from exc


async def _record(store: Store, session: Session, kind: str, label: str, **detail: Any) -> None:
    """Append a timeline event attributed to the agent.

    `origin=ORIGIN` is a module constant, not an argument, so no endpoint in
    this file can be made to write a HUMAN-origin event.
    """
    await store.append_event(
        RunEvent(session_id=session.id, kind=kind, label=label, detail=detail, origin=ORIGIN)
    )


# -- read tools (F7-02) -----------------------------------------------------


class ProjectStatusResponse(BaseModel):
    """What an agent may know about a project. Ownership is enforced by the
    store's query, not by filtering here."""

    project_id: str
    name: str
    state: str
    access_mode: str
    repository_full_name: str | None
    is_demo: bool
    session_id: str


@router.get("/sessions/{session_id}/status", response_model=ProjectStatusResponse)
async def get_project_status(
    session_id: str, identity: CurrentIdentity, request: Request
) -> ProjectStatusResponse:
    session = await _session(request, session_id, identity)
    project = await _project(request, session.project_id, identity)
    return ProjectStatusResponse(
        project_id=project.id,
        name=project.name,
        state=session.state.value,
        access_mode=project.access_mode.value,
        repository_full_name=project.repository_full_name,
        is_demo=project.is_demo,
        session_id=session.id,
    )


class ArtifactResponse(BaseModel):
    """A stored artifact, with the hash an approval would bind to.

    `available` is false rather than 404 when the step has not run: an agent
    asking "is there a plan yet?" is a normal question, not an error.
    """

    available: bool
    kind: str
    artifact_hash: str | None
    payload: dict[str, Any]


async def _artifact_response(
    request: Request, session: Session, kind: ArtifactKind, identity: CurrentIdentity
) -> ArtifactResponse:
    artifact = await _store(request).get_artifact(session.id, kind, identity.subject)
    if artifact is None:
        return ArtifactResponse(available=False, kind=kind.value, artifact_hash=None, payload={})
    return ArtifactResponse(
        available=True, kind=kind.value, artifact_hash=artifact.hash, payload=artifact.payload
    )


@router.get("/sessions/{session_id}/workflows", response_model=ArtifactResponse)
async def list_detected_workflows(
    session_id: str, identity: CurrentIdentity, request: Request
) -> ArtifactResponse:
    session = await _session(request, session_id, identity)
    return await _artifact_response(request, session, ArtifactKind.ANALYSIS, identity)


@router.get("/sessions/{session_id}/plan", response_model=ArtifactResponse)
async def get_webmcp_plan(
    session_id: str, identity: CurrentIdentity, request: Request
) -> ArtifactResponse:
    session = await _session(request, session_id, identity)
    return await _artifact_response(request, session, ArtifactKind.TOOL_PLAN, identity)


@router.get("/sessions/{session_id}/validation", response_model=ArtifactResponse)
async def get_validation_report(
    session_id: str, identity: CurrentIdentity, request: Request
) -> ArtifactResponse:
    session = await _session(request, session_id, identity)
    return await _artifact_response(request, session, ArtifactKind.VALIDATION, identity)


class StageResponse(BaseModel):
    """The result of asking for a pipeline stage.

    `started` is false while the orchestrator is not wired to these endpoints.
    Returning true would be a hardcoded value that makes a check look passed —
    CLAUDE.md §9. The agent is told plainly instead, so it does not wait for a
    result that will never arrive.
    """

    session_id: str
    state: str
    started: bool
    detail: str


#: Said the same way by every stage that is recorded but not yet executed.
NOT_WIRED = (
    "The request was recorded on the timeline, but this stage is not yet "
    "connected to the orchestrator, so nothing ran. Tracked as F9-01."
)


@router.post("/sessions/{session_id}/analysis", response_model=StageResponse)
async def start_repository_analysis(
    session_id: str, identity: CurrentIdentity, request: Request
) -> StageResponse:
    """Analysis reads the repository and writes nothing to it, so it needs no
    gate. It is recorded as an agent action, and reports honestly that the
    orchestrator does not yet act on the request."""
    session = await _session(request, session_id, identity)
    store = _store(request)
    await _record(store, session, "analysis.requested", "Agent requested repository analysis")
    return StageResponse(
        session_id=session.id, state=session.state.value, started=False, detail=NOT_WIRED
    )


# -- gated mutation tools (F7-03) -------------------------------------------


class AwaitingApprovalResponse(BaseModel):
    """The only thing a mutation tool can return.

    There is deliberately no success variant. An agent calling one of these
    tools has not caused the action to happen; it has asked a human to decide.
    """

    status: str = "awaiting_human_approval"
    approval_id: str
    gate: ApprovalGate
    artifact_hash: str
    message: str


async def _await_human(
    request: Request,
    session: Session,
    *,
    kind: ArtifactKind,
    gate: ApprovalGate,
    payload: dict[str, Any],
    summary: str,
) -> AwaitingApprovalResponse:
    """Record the artifact, open the gate request, and stop.

    This is the single path every mutation tool takes. It calls the same
    `open_gate_request` the human UI calls — there is no agent-only variant of
    approval creation, which is what makes the gate impossible to route around.
    """
    store = _store(request)
    artifact = await store.put_artifact(
        Artifact(session_id=session.id, project_id=session.project_id, kind=kind, payload=payload)
    )
    approval = await open_gate_request(
        store,
        session,
        gate=gate,
        artifact_hash=artifact.hash,  # derived from stored content, never supplied
        summary=summary,
    )
    await _record(
        store,
        session,
        "approval.requested",
        f"Agent requested your decision: {gate.value}",
        approval_id=approval.id,
    )
    log.info(
        "agent.awaiting_approval",
        session_id=session.id,
        gate=gate.value,
        approval_id=approval.id,
    )
    return AwaitingApprovalResponse(
        approval_id=approval.id,
        gate=gate,
        artifact_hash=artifact.hash,
        message=(
            "This action is waiting for a human decision in MCPForge. "
            "Nothing has been changed. An agent cannot approve it."
        ),
    )


class ConnectProjectBody(BaseModel):
    repository_full_name: str = Field(min_length=1, max_length=200)
    branch: str = Field(min_length=1, max_length=200)


@router.post("/sessions/{session_id}/repository", response_model=AwaitingApprovalResponse)
async def connect_project(
    session_id: str, body: ConnectProjectBody, identity: CurrentIdentity, request: Request
) -> AwaitingApprovalResponse:
    session = await _session(request, session_id, identity)
    return await _await_human(
        request,
        session,
        kind=ArtifactKind.REPOSITORY_BINDING,
        gate=ApprovalGate.REPOSITORY_BINDING,
        payload={"repository_full_name": body.repository_full_name, "branch": body.branch},
        summary=f"Bind this project to {body.repository_full_name} @ {body.branch}",
    )


class SelectWorkflowsBody(BaseModel):
    workflow_ids: list[str] = Field(min_length=1, max_length=50)


@router.post("/sessions/{session_id}/workflows", response_model=AwaitingApprovalResponse)
async def select_workflows(
    session_id: str, body: SelectWorkflowsBody, identity: CurrentIdentity, request: Request
) -> AwaitingApprovalResponse:
    session = await _session(request, session_id, identity)
    return await _await_human(
        request,
        session,
        kind=ArtifactKind.WORKFLOW_SELECTION,
        gate=ApprovalGate.WORKFLOW_SELECTION,
        payload={"workflow_ids": sorted(body.workflow_ids)},
        summary=f"Expose {len(body.workflow_ids)} workflow(s) as WebMCP tools",
    )


@router.post("/sessions/{session_id}/plan/approve", response_model=AwaitingApprovalResponse)
async def approve_webmcp_plan(
    session_id: str, identity: CurrentIdentity, request: Request
) -> AwaitingApprovalResponse:
    """Named for the agent's intent, not its effect.

    An agent calling this does **not** approve the plan. It asks a human to. The
    endpoint takes no body at all, so there is no field an agent could set to
    express a decision, and nothing it says is read.
    """
    session = await _session(request, session_id, identity)
    store = _store(request)
    plan = await store.get_artifact(session.id, ArtifactKind.TOOL_PLAN, identity.subject)
    if plan is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "There is no tool plan to request approval for yet"
        )
    approval = await open_gate_request(
        store,
        session,
        gate=ApprovalGate.TOOL_PLAN,
        artifact_hash=plan.hash,
        summary="Approve the WebMCP tool plan",
    )
    await _record(
        store,
        session,
        "approval.requested",
        "Agent requested your decision: TOOL_PLAN",
        approval_id=approval.id,
    )
    return AwaitingApprovalResponse(
        approval_id=approval.id,
        gate=ApprovalGate.TOOL_PLAN,
        artifact_hash=plan.hash,
        message=(
            "The plan is waiting for your approval in MCPForge. "
            "An agent cannot approve its own plan."
        ),
    )


class GeneratePatchBody(BaseModel):
    summary: str = Field(default="Generate the WebMCP patch", max_length=500)


@router.post("/sessions/{session_id}/patch", response_model=StageResponse)
async def generate_patch(
    session_id: str, body: GeneratePatchBody, identity: CurrentIdentity, request: Request
) -> StageResponse:
    """Request patch generation.

    Generation is authorised by the TOOL_PLAN approval, not by a gate of its
    own, so this opens no approval. It deliberately does **not** create a PATCH
    approval either: a PATCH approval must bind to `artifact_hash(patch.hashable())`
    (`github/writer.py`), and no patch exists yet. Binding one to a placeholder
    would give the developer something to approve that could never authorise the
    write — an approval that reads as granted and is inert.
    """
    session = await _session(request, session_id, identity)
    store = _store(request)
    plan = await store.get_artifact(session.id, ArtifactKind.TOOL_PLAN, identity.subject)
    if plan is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Generate a tool plan first")
    await _record(
        store,
        session,
        "generation.requested",
        "Agent requested patch generation",
        summary=body.summary,
    )
    return StageResponse(
        session_id=session.id, state=session.state.value, started=False, detail=NOT_WIRED
    )


@router.post("/sessions/{session_id}/security-review", response_model=StageResponse)
async def run_security_review(
    session_id: str, identity: CurrentIdentity, request: Request
) -> StageResponse:
    """Request a security review.

    The policy engine exists and is tested (`security/policy.py`), but nothing
    here invokes it yet, so this reports that rather than returning an empty
    report that would read as a clean review.
    """
    session = await _session(request, session_id, identity)
    store = _store(request)
    await _record(store, session, "security_review.requested", "Agent requested a security review")
    return StageResponse(
        session_id=session.id, state=session.state.value, started=False, detail=NOT_WIRED
    )


@router.post("/sessions/{session_id}/validation", response_model=StageResponse)
async def run_validation(
    session_id: str, identity: CurrentIdentity, request: Request
) -> StageResponse:
    """Request validation. The sandbox executor exists; this does not yet call
    it, and says so rather than returning an empty pass."""
    session = await _session(request, session_id, identity)
    store = _store(request)
    await _record(store, session, "validation.requested", "Agent requested validation")
    return StageResponse(
        session_id=session.id, state=session.state.value, started=False, detail=NOT_WIRED
    )


class CreatePullRequestBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=20000)


@router.post("/sessions/{session_id}/pull-request", response_model=AwaitingApprovalResponse)
async def create_pull_request(
    session_id: str, body: CreatePullRequestBody, identity: CurrentIdentity, request: Request
) -> AwaitingApprovalResponse:
    """The most consequential tool, and still only a request.

    The write itself remains behind `github/writer.py`'s own refusals, which
    re-check the policy engine and both approvals at write time. This endpoint
    cannot reach them.
    """
    session = await _session(request, session_id, identity)
    store = _store(request)
    patch = await store.get_artifact(session.id, ArtifactKind.PATCH, identity.subject)
    if patch is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "There is no patch to open a PR for")
    # Bound to the patch's own hash, not to a new object wrapping it.
    # `github/writer.py` requires the PATCH and PULL_REQUEST approvals to cover
    # the same `artifact_hash(patch.hashable())`; an approval bound to anything
    # else is inert, and the developer would have approved nothing.
    approval = await open_gate_request(
        store,
        session,
        gate=ApprovalGate.PULL_REQUEST,
        artifact_hash=patch.hash,
        summary=f"Open a pull request: {body.title}",
    )
    await _record(
        store,
        session,
        "approval.requested",
        "Agent requested your decision: PULL_REQUEST",
        approval_id=approval.id,
        pr_title=body.title,
    )
    return AwaitingApprovalResponse(
        approval_id=approval.id,
        gate=ApprovalGate.PULL_REQUEST,
        artifact_hash=patch.hash,
        message=(
            "The pull request is waiting for your approval in MCPForge. An agent cannot approve it."
        ),
    )
