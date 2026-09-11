"""The orchestrator, connected to the stages — ticket F9-01.

Until this module the state machine (`machine.py`) and the stages (the agents,
the generator, the validator, the writer) were each tested and never joined: no
stage persisted an artifact, and nothing consumed an approved decision. This is
the join. Each method is one stage, and each does the same four things in the
same order:

1. refuse unless the session is in a state this stage may start from;
2. move the session through `RunMachine.transition` — the only way state ever
   changes, and the place every gate is checked against a stored `Approval`;
3. run the stage, persisting its artifact **at the point it is produced**;
4. move the session on, or record the failure and stop.

What this module deliberately does not do:

- **Create or decide an approval.** When a stage reaches a gate it returns a
  `GateRequest`; the router opens it through `api/approvals.open_gate_request`,
  the one creation path. Deciding stays in `api/approvals.decide_approval`.
- **Read model output as authorization.** Every gate is opened by a stored
  record the machine loads by id. The security agent's verdict is advisory
  input to `evaluate_gate`; the validator's verdict is exit codes.
- **Reach a pull request for a demo project.** `request_pull_request` applies
  the writer's own boundary checks before any transition, so a demo run stops at
  `VALIDATION_PASSED`, and the writer refuses it again if called directly.

The `ArtifactKind.PATCH` payload is exactly `GeneratedPatch.hashable()`, which is
the value `github/writer.py` requires both approvals to cover. Generation is
deterministic, so the full patch is rebuilt from the approved plan when a later
stage needs it, and refused if it no longer hashes to what was approved.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio.to_thread
import httpx
from pydantic import ValidationError

from mcpforge.agents.analyst import AnalystInput, CodebaseAnalyst
from mcpforge.agents.architect import ArchitectInput, WorkflowArchitect
from mcpforge.agents.base import AgentError
from mcpforge.agents.security_reviewer import (
    SecurityReviewer,
    SecurityReviewInput,
    evaluate_gate,
)
from mcpforge.api.trust import QUARANTINE_FIELD
from mcpforge.execution.provider import (
    Command,
    SandboxError,
    SecureExecutionProvider,
    Workspace,
    WorkspaceSpec,
    resolve_inside,
)
from mcpforge.gemini.provider import GeminiProvider, TraceContext
from mcpforge.generation.nextjs import GeneratedSecretError, generate_patch
from mcpforge.github.boundary import (
    AccessModeError,
    BoundaryError,
    assert_may_write,
    assert_within_boundary,
    bind_repository,
)
from mcpforge.github.branches import branch_name_for
from mcpforge.github.client import (
    GitHubAppClient,
    GitHubError,
    InstallationToken,
    Repository,
)
from mcpforge.github.writer import BranchAndPullRequestWriter, PullRequest, WriteRefusedError
from mcpforge.indexing.indexer import build_index
from mcpforge.indexing.retrieval import (
    BudgetExceededError,
    ContextRetriever,
    RetrievalRequest,
)
from mcpforge.indexing.sources import CHECKOUT_DIR, DemoSource, GitHubSource, IngestionError
from mcpforge.logging import get_logger
from mcpforge.models.analysis import CodebaseAnalysis
from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    Origin,
    Project,
    RunEvent,
    RunState,
    Session,
)
from mcpforge.models.index import FileKind, RepositoryIndex
from mcpforge.models.patch import GeneratedPatch
from mcpforge.models.toolplan import ToolPlan
from mcpforge.models.transitions import LEGAL, ApprovalRequiredError, gate_for
from mcpforge.models.webmcp import WebMCPToolset
from mcpforge.orchestration.dependencies import install_dependencies
from mcpforge.orchestration.machine import RetryLimitExceededError, RunMachine
from mcpforge.orchestration.toolset import ToolsetConversionError, toolset_from_plan
from mcpforge.orchestration.validation_run import ValidationOutcome, validate_workspace
from mcpforge.store.port import NotFoundError, Store

log = get_logger(__name__)

S = RunState

#: `services/api/src/mcpforge/orchestration/pipeline.py` -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[5]

#: The bundled demo application (`01_PRD.md` §7).
DEFAULT_DEMO_FIXTURE = REPO_ROOT / "fixtures" / "demo-hotel-app"

#: The demo fixture is an npm workspace of this repository, so its dependency
#: tree is the repository's own `node_modules`, installed by `npm ci`. A
#: connected GitHub repository gets its tree from the separate, networked
#: install step instead (`orchestration/dependencies.py`,
#: `03_SECURITY_ACCESS.md` §3).
DEFAULT_DEMO_DEPENDENCY_TREE = REPO_ROOT / "node_modules"

#: Retrieval budget for the analyst's source snippets, in estimated tokens.
ANALYST_CONTEXT_BUDGET = 8000

DEFAULT_VALIDATION_TIMEOUT_SECONDS = 900

#: Each human gate, the approval gate that exits it, and the artifact it binds.
#: The pull-request gate binds to the PATCH artifact: `github/writer.py`
#: requires both approvals to cover the same `artifact_hash(patch.hashable())`.
PENDING_GATES: dict[RunState, tuple[ApprovalGate, ArtifactKind]] = {
    S.TOOL_PLAN_APPROVAL_PENDING: (ApprovalGate.TOOL_PLAN, ArtifactKind.TOOL_PLAN),
    S.PATCH_APPROVAL_PENDING: (ApprovalGate.PATCH, ArtifactKind.PATCH),
    S.PR_APPROVAL_PENDING: (ApprovalGate.PULL_REQUEST, ArtifactKind.PATCH),
}


def rejection_target(pending: RunState) -> RunState:
    """Where rejecting a gate returns to, read from the transition table.

    Derived rather than restated: the table's non-gated successor of a pending
    state is its rejection path, so there is one source for it.
    """
    targets = [s for s in LEGAL[pending] if gate_for(s) is None]
    if len(targets) != 1:
        raise ValueError(f"{pending.value} has no single rejection path in the table")
    return targets[0]


# ---------------------------------------------------------------------------
# Errors and results
# ---------------------------------------------------------------------------


class PipelineError(Exception):
    """A stage could not run or could not finish. The message is user-facing."""


class StageStateError(PipelineError):
    """The run is not in a state this stage may start from."""


class PipelineUnavailableError(PipelineError):
    """Something the stage needs is not configured on this deployment."""


class ApprovalNotCurrentError(PipelineError):
    """The approval predates the run's latest arrival at this gate.

    A gate re-entered after a rejection needs a new decision. Without this, an
    approval given before the rejection would still cover the unchanged artifact
    and carry the run straight back through a gate the developer had just
    sent it back from.
    """


class PullRequestRefusedError(PipelineError):
    """The project may not open a pull request: a demo, or not elevated."""


class CouldNotValidateError(Exception):
    """Validation could not be performed — the dependency install was refused
    or failed — as distinct from a validation that ran and failed. Carries the
    labelled dependency record so it is stored either way."""

    def __init__(self, reason: str, dependencies: dict[str, Any]) -> None:
        super().__init__(reason)
        self.dependencies = dependencies


@dataclass(frozen=True)
class GateRequest:
    """A gate the router must open, through `open_gate_request`, for the human."""

    gate: ApprovalGate
    artifact_hash: str
    summary: str


@dataclass(frozen=True)
class StepResult:
    session: Session
    detail: str
    gate_request: GateRequest | None = None
    pull_request_url: str | None = None


@dataclass(frozen=True)
class Actor:
    """Who caused a transition. Supplied by the router from the verified token."""

    name: str
    origin: Origin


@dataclass(frozen=True)
class PipelineOptions:
    """Where the pipeline finds things. Set once, in `create_app`.

    None of these can open a gate or change what a stage decides. They say
    where the demo fixture and its dependency tree are, how long validation may
    take, and which HTTP transport the pull-request writer uses — `None` is the
    real network, and a test supplies a recorded transport exactly as it does
    for `GitHubAppClient(http=...)`.
    """

    demo_fixture: Path = DEFAULT_DEMO_FIXTURE
    demo_dependency_tree: Path | None = DEFAULT_DEMO_DEPENDENCY_TREE
    validation_timeout_seconds: int = DEFAULT_VALIDATION_TIMEOUT_SECONDS
    writer_transport: httpx.AsyncBaseTransport | None = None


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


class Pipeline:
    """Every stage of one run, each one guarded by `RunMachine`."""

    def __init__(
        self,
        *,
        store: Store,
        gemini: GeminiProvider,
        executor: SecureExecutionProvider | None,
        github: GitHubAppClient,
        options: PipelineOptions | None = None,
    ) -> None:
        resolved = options if options is not None else PipelineOptions()
        self._store = store
        self._gemini = gemini
        self._executor = executor
        self._github = github
        self._demo_fixture = resolved.demo_fixture
        self._demo_dependency_tree = resolved.demo_dependency_tree
        self._validation_timeout = resolved.validation_timeout_seconds
        self._writer_transport = resolved.writer_transport
        self._machine = RunMachine(store)

    # -- plumbing ----------------------------------------------------------

    async def _load(self, session_id: str, owner: str) -> tuple[Session, Project]:
        session = await self._store.get_session(session_id, owner)
        project = await self._store.get_project(session.project_id, owner)
        return session, project

    @staticmethod
    def _expect(session: Session, *states: RunState) -> None:
        if session.state not in states:
            allowed = ", ".join(s.value for s in states)
            raise StageStateError(
                f"This step can start only from {allowed}; the run is in {session.state.value}."
            )

    def _require_executor(self) -> SecureExecutionProvider:
        if self._executor is None:
            raise PipelineUnavailableError(
                "No secure execution provider is attached to this deployment, so no "
                "repository job can run. Nothing was started."
            )
        return self._executor

    async def _move(
        self,
        session: Session,
        target: RunState,
        actor: Actor,
        cause: str,
        *,
        approval_id: str | None = None,
        artifact_hash: str | None = None,
    ) -> Session:
        return await self._machine.transition(
            session,
            target,
            actor=actor.name,
            origin=actor.origin,
            cause=cause,
            approval_id=approval_id,
            artifact_hash=artifact_hash,
        )

    async def _event(self, session: Session, kind: str, label: str, **detail: Any) -> None:
        await self._store.append_event(
            RunEvent(
                session_id=session.id, kind=kind, label=label, detail=detail, origin=Origin.SYSTEM
            )
        )

    async def _fail(self, session: Session, step: str, exc: BaseException) -> PipelineError:
        """Record a failed step on the timeline and build the error to raise.

        The session stays where it is — the running state, whose retry
        self-loop is how the developer tries again.
        """
        reason = str(exc)[:400]
        await self._event(session, "step.failed", f"{step} failed", reason=reason)
        log.warning("pipeline.step_failed", session_id=session.id, step=step)
        return PipelineError(f"{step} failed: {reason}")

    async def _artifact(self, session: Session, kind: ArtifactKind) -> Artifact:
        artifact = await self._store.get_artifact(session.id, kind, session.owner_uid)
        if artifact is None:
            raise StageStateError(f"There is no {kind.value} artifact for this run yet.")
        return artifact

    async def _entered_at(self, session: Session, state: RunState) -> datetime:
        """When the run most recently arrived in `state`, from its own timeline."""
        if state is S.PROJECT_CREATED:
            return session.created_at
        events = await self._store.list_events(session.id, session.owner_uid)
        arrivals = [
            e.created_at
            for e in events
            if e.kind == "state.changed" and e.detail.get("to") == state.value
        ]
        if not arrivals:
            raise StageStateError(f"The run has no record of reaching {state.value}.")
        return arrivals[-1]

    async def _require_current(self, session: Session, approval_id: str, state: RunState) -> None:
        """Refuse an approval requested before the run last reached this gate."""
        try:
            approval = await self._store.get_approval(approval_id, session.owner_uid)
        except NotFoundError as exc:
            gate = PENDING_GATES.get(state, (ApprovalGate.WORKFLOW_SELECTION, None))[0]
            raise ApprovalRequiredError(state, gate) from exc
        if approval.requested_at < await self._entered_at(session, state):
            raise ApprovalNotCurrentError(
                f"Approval {approval.id} was requested before the run last reached "
                f"{state.value}. A gate reached again needs a new decision."
            )

    @staticmethod
    def _trace(session: Session, step: str) -> TraceContext:
        return TraceContext(
            project_id=session.project_id, run_id=session.id, agent="orchestrator", step=step
        )

    async def _analysis(
        self, session: Session
    ) -> tuple[CodebaseAnalysis, RepositoryIndex, str | None]:
        artifact = await self._artifact(session, ArtifactKind.ANALYSIS)
        try:
            analysis = CodebaseAnalysis.model_validate(artifact.payload["analysis"])
            index = RepositoryIndex.model_validate(artifact.payload["index"])
        except (KeyError, ValidationError) as exc:
            raise PipelineError(
                "The stored analysis is not in the shape this stage reads."
            ) from exc
        base = artifact.payload.get("base_commit")
        return analysis, index, base if isinstance(base, str) else None

    async def _plan(self, session: Session) -> tuple[ToolPlan, Artifact]:
        artifact = await self._artifact(session, ArtifactKind.TOOL_PLAN)
        try:
            return ToolPlan.model_validate(artifact.payload["plan"]), artifact
        except (KeyError, ValidationError) as exc:
            raise PipelineError(
                "The stored tool plan is not in the shape this stage reads."
            ) from exc

    @staticmethod
    def _build(
        plan: ToolPlan, index: RepositoryIndex, base_commit: str | None
    ) -> tuple[WebMCPToolset, GeneratedPatch]:
        toolset = toolset_from_plan(plan, index)
        return toolset, generate_patch(toolset, base_commit=base_commit)

    async def _approved_patch(
        self, session: Session
    ) -> tuple[WebMCPToolset, GeneratedPatch, ToolPlan, Artifact]:
        """Rebuild the patch from the approved plan, and prove it is the stored one."""
        _, index, base = await self._analysis(session)
        plan, _ = await self._plan(session)
        stored = await self._artifact(session, ArtifactKind.PATCH)
        toolset, patch = self._build(plan, index, base)
        if (
            Artifact(
                session_id=session.id,
                project_id=session.project_id,
                kind=ArtifactKind.PATCH,
                payload=patch.hashable(),
            ).hash
            != stored.hash
        ):
            raise PipelineError(
                "The plan no longer generates the patch that is stored for this run, so the "
                "patch cannot be used. Regenerate it."
            )
        return toolset, patch, plan, stored

    async def read_patch(
        self, session_id: str, owner: str
    ) -> tuple[GeneratedPatch, Artifact] | None:
        """The stored patch, rebuilt in full for display — ticket T3. Read-only.

        The PATCH artifact holds only `GeneratedPatch.hashable()`, which has no
        rationale. The full patch is rebuilt from the approved plan exactly as a
        later stage does, and refused (`PipelineError`) if it no longer hashes
        to the stored one — so what is shown is what an approval covers.

        `None` when no patch has been generated. No transition, no event.
        """
        session = await self._store.get_session(session_id, owner)
        if await self._store.get_artifact(session.id, ArtifactKind.PATCH, owner) is None:
            return None
        _, patch, _, stored = await self._approved_patch(session)
        return patch, stored

    async def _installation_repository(
        self, full_name: str
    ) -> tuple[Repository, InstallationToken]:
        """The repository as the App installation sees it, and a fresh token.

        Only repositories the installation was granted are found. A project
        bound to anything else is refused rather than reached another way.
        """
        if not self._github.configured:
            raise PipelineUnavailableError("GitHub is not configured on this deployment.")
        try:
            for installation in await self._github.list_installations():
                token = await self._github.create_installation_token(installation.id)
                for repository in await self._github.list_repositories(token):
                    if repository.full_name == full_name:
                        return repository, token
        except GitHubError as exc:
            raise PipelineError(f"GitHub could not be reached: {exc}") from exc
        raise PipelineError(
            f"{full_name} is not a repository the MCPForge GitHub App installation was granted."
        )

    async def _materialise(
        self, project: Project, executor: SecureExecutionProvider, workspace: Workspace
    ) -> str | None:
        """Place the project's source at `<workspace>/repo`; return its commit.

        The demo fixture is a local copy with no commit. A GitHub repository is
        cloned in a separate, networked workspace — the clone is the one
        networked operation (`03_SECURITY_ACCESS.md` §3) — and copied across, so
        the workspace every analysis and validation command runs in never has
        the network.
        """
        if project.is_demo:
            await DemoSource(self._demo_fixture).fetch(executor, workspace)
            return None

        if project.repository_full_name is None or project.base_branch is None:
            raise PipelineError("The project records no repository branch to read.")
        _, token = await self._installation_repository(project.repository_full_name)
        clone = await executor.create_workspace(
            WorkspaceSpec(run_id=f"{workspace.id}-clone", allow_network=True)
        )
        try:
            checkout = await GitHubSource(
                full_name=project.repository_full_name,
                branch=project.base_branch,
                token=token,
            ).fetch(executor, clone)
            head = await executor.run(
                clone,
                Command(argv=("git", "rev-parse", "HEAD"), cwd=CHECKOUT_DIR, timeout_seconds=60),
            )
            commit = head.stdout.strip()
            if not head.ok or not commit:
                raise IngestionError("The cloned repository reported no commit.")
            destination = resolve_inside(workspace, CHECKOUT_DIR)

            def copy() -> None:
                shutil.copytree(
                    checkout.path,
                    destination,
                    symlinks=True,
                    # A shipped `node_modules` is never used: dependencies come
                    # from the install step (`orchestration/dependencies.py`) or
                    # not at all, so the report can say where they came from.
                    ignore=shutil.ignore_patterns(".git", "node_modules"),
                )

            await anyio.to_thread.run_sync(copy)
        finally:
            await executor.destroy(clone)
        return commit

    def _link_dependencies(self, project: Project, workspace: Workspace) -> bool:
        """Give the demo fixture its dependency tree. Returns whether one was linked.

        Hard links, not a symlink: Turbopack refuses a `node_modules` symlink
        that leaves the project root, and a copy is hundreds of megabytes. Falls
        back to a copy across filesystems. A GitHub repository gets nothing —
        see `DEFAULT_DEMO_DEPENDENCY_TREE`.
        """
        tree = self._demo_dependency_tree
        if not project.is_demo or tree is None or not tree.is_dir():
            return False

        def link(src: str, dst: str) -> object:
            try:
                os.link(src, dst)
            except OSError:
                return shutil.copy2(src, dst)
            return dst

        shutil.copytree(
            tree,
            resolve_inside(workspace, f"{CHECKOUT_DIR}/node_modules"),
            symlinks=True,
            copy_function=link,
        )
        return True

    # -- stages ------------------------------------------------------------

    async def connect(
        self,
        session_id: str,
        owner: str,
        actor: Actor,
        *,
        repository_binding_approval_id: str | None = None,
    ) -> StepResult:
        """Leave `PROJECT_CREATED`.

        A demo project takes the table's demo shortcut straight to
        `ANALYSIS_PENDING`. A repository project passes through
        `REPOSITORY_CONNECTED`. With an approval id, an agent's
        `REPOSITORY_BINDING` request is consumed: the approval is checked by the
        machine against the stored request, the repository is resolved through
        the App installation, and the project is bound — never rebound.
        """
        session, project = await self._load(session_id, owner)
        self._expect(session, S.PROJECT_CREATED)

        if repository_binding_approval_id is not None:
            if not project.is_demo:
                raise StageStateError("This project is already bound to a repository.")
            request = await self._artifact(session, ArtifactKind.REPOSITORY_BINDING)
            await self._machine.check_approval(
                session,
                ApprovalGate.REPOSITORY_BINDING,
                for_state=S.REPOSITORY_CONNECTED,
                approval_id=repository_binding_approval_id,
                artifact_hash=request.hash,
            )
            full_name = request.payload.get("repository_full_name")
            branch = request.payload.get("branch")
            if not isinstance(full_name, str) or not isinstance(branch, str):
                raise PipelineError("The stored repository request is malformed.")
            repository, _ = await self._installation_repository(full_name)
            try:
                project = bind_repository(
                    project,
                    repository_id=str(repository.id),
                    full_name=repository.full_name,
                    base_branch=branch,
                )
            except BoundaryError as exc:
                raise PipelineError(str(exc)) from exc
            project = await self._store.update_project(project)

        if project.is_demo:
            session = await self._move(
                session,
                S.ANALYSIS_PENDING,
                actor,
                "Demo project: there is no repository to connect",
            )
            return StepResult(session, "The demo project is ready to analyze.")

        session = await self._move(
            session,
            S.REPOSITORY_CONNECTED,
            actor,
            f"Connected to {project.repository_full_name} @ {project.base_branch}",
        )
        session = await self._move(session, S.ANALYSIS_PENDING, actor, "Ready to analyze")
        return StepResult(session, f"{project.repository_full_name} is ready to analyze.")

    async def analyze(self, session_id: str, owner: str, actor: Actor) -> StepResult:
        """Ingest, filter, index, retrieve, and ask agent 1 for workflows.

        `Repository → deterministic index → relevant context → Gemini`: the
        analyst sees the structural index and a budgeted set of snippets the
        retriever selected from files the filter let through, never the tree.
        """
        session, project = await self._load(session_id, owner)
        self._expect(session, S.ANALYSIS_PENDING, S.ANALYSIS_RUNNING)
        cause = "Retrying analysis" if session.state is S.ANALYSIS_RUNNING else "Analysis started"
        session = await self._move(session, S.ANALYSIS_RUNNING, actor, cause)
        executor = self._require_executor()

        workspace = await executor.create_workspace(WorkspaceSpec(run_id=f"{session.id}-analysis"))
        try:
            base_commit = await self._materialise(project, executor, workspace)
            root = resolve_inside(workspace, CHECKOUT_DIR)
            index = await anyio.to_thread.run_sync(build_index, root)
            if not index.framework.supported:
                raise PipelineError(index.framework.reason or "The framework is not supported.")
            context = (
                ContextRetriever(index, root)
                .retrieve(
                    RetrievalRequest(
                        preferred_kinds=[FileKind.SERVICE, FileKind.API_HANDLER],
                        token_budget=ANALYST_CONTEXT_BUDGET,
                    )
                )
                .render()
            )
        except (PipelineError, IngestionError, SandboxError, BudgetExceededError, OSError) as exc:
            raise await self._fail(session, "Repository analysis", exc) from exc
        finally:
            await executor.destroy(workspace)

        try:
            analysis, _ = await CodebaseAnalyst(self._gemini).run(
                AnalystInput(index=index, context=context),
                self._trace(session, "Identifying workflows"),
            )
        except AgentError as exc:
            raise await self._fail(session, "Identifying workflows", exc) from exc

        # The index stores structure and no file bodies. Its root is the
        # destroyed workspace, so it is recorded as the checkout directory.
        stored_index = index.model_copy(update={"root": CHECKOUT_DIR})
        await self._store.put_artifact(
            Artifact(
                session_id=session.id,
                project_id=session.project_id,
                kind=ArtifactKind.ANALYSIS,
                payload={
                    "source": "demo" if project.is_demo else "github",
                    "base_commit": base_commit,
                    "analysis": analysis.model_dump(mode="json"),
                    "index": stored_index.model_dump(mode="json"),
                    QUARANTINE_FIELD: list(index.quarantined_paths),
                },
            )
        )
        await self._event(
            session,
            "artifact.ready",
            "Analysis ready",
            files_indexed=len(index.files),
            workflows=len(analysis.workflows),
            quarantined=len(index.quarantined_paths),
        )
        session = await self._move(session, S.ANALYSIS_COMPLETE, actor, "Analysis complete")
        session = await self._move(
            session, S.WORKFLOW_SELECTION_PENDING, actor, "Waiting for the developer's selection"
        )
        return StepResult(
            session, f"{len(analysis.workflows)} workflow(s) found in {len(index.files)} files."
        )

    async def select_workflows(
        self,
        session_id: str,
        owner: str,
        actor: Actor,
        *,
        workflow_ids: list[str] | None = None,
        approval_id: str | None = None,
    ) -> StepResult:
        """Record the developer's selection.

        Two ways, and exactly one per call. The developer selects directly — the
        click is the decision (`02_ARCHITECTURE.md` §10.2) — or approves an
        agent's `WORKFLOW_SELECTION` request, in which case the selection is the
        stored request and the approval must cover it, checked by the machine.
        """
        if (workflow_ids is None) == (approval_id is None):
            raise PipelineError("Give either the workflows to select or an approval id, not both.")
        session, _ = await self._load(session_id, owner)
        self._expect(session, S.WORKFLOW_SELECTION_PENDING)
        analysis, _, _ = await self._analysis(session)

        if approval_id is not None:
            request = await self._artifact(session, ArtifactKind.WORKFLOW_SELECTION)
            await self._require_current(session, approval_id, S.WORKFLOW_SELECTION_PENDING)
            await self._machine.check_approval(
                session,
                ApprovalGate.WORKFLOW_SELECTION,
                for_state=S.WORKFLOWS_SELECTED,
                approval_id=approval_id,
                artifact_hash=request.hash,
            )
            raw = request.payload.get("workflow_ids")
            if not isinstance(raw, list) or not all(isinstance(i, str) for i in raw):
                raise PipelineError("The stored workflow selection is malformed.")
            selected = sorted(set(raw))
        else:
            assert workflow_ids is not None
            selected = sorted(set(workflow_ids))

        known = {w.id for w in analysis.workflows}
        unknown = sorted(set(selected) - known)
        if not selected or unknown:
            raise PipelineError(
                f"Select at least one discovered workflow. Not in this analysis: {unknown}"
            )

        if approval_id is None:
            await self._store.put_artifact(
                Artifact(
                    session_id=session.id,
                    project_id=session.project_id,
                    kind=ArtifactKind.WORKFLOW_SELECTION,
                    payload={"workflow_ids": selected},
                )
            )
        session = await self._move(
            session, S.WORKFLOWS_SELECTED, actor, f"{len(selected)} workflow(s) selected"
        )
        return StepResult(session, f"Selected: {', '.join(selected)}")

    async def plan(self, session_id: str, owner: str, actor: Actor) -> StepResult:
        """Agent 2 designs the tools; the plan is checked generatable, then gated."""
        session, _ = await self._load(session_id, owner)
        self._expect(session, S.WORKFLOWS_SELECTED, S.TOOL_PLAN_RUNNING)
        cause = "Retrying the tool plan" if session.state is S.TOOL_PLAN_RUNNING else "Designing"
        session = await self._move(session, S.TOOL_PLAN_RUNNING, actor, cause)

        analysis, index, _ = await self._analysis(session)
        selection = await self._artifact(session, ArtifactKind.WORKFLOW_SELECTION)
        selected = selection.payload.get("workflow_ids")
        if not isinstance(selected, list) or not selected:
            raise PipelineError("The stored workflow selection is malformed.")

        try:
            plan, discrepancies, _ = await WorkflowArchitect(self._gemini).design(
                ArchitectInput(
                    index=index, analysis=analysis, selected_workflow_ids=[str(i) for i in selected]
                ),
                self._trace(session, "Designing WebMCP tools"),
            )
            # Refused here, before a human is asked to approve something that
            # cannot be generated.
            bound = toolset_from_plan(plan, index)
        except (AgentError, ToolsetConversionError) as exc:
            raise await self._fail(session, "Designing WebMCP tools", exc) from exc

        artifact = await self._store.put_artifact(
            Artifact(
                session_id=session.id,
                project_id=session.project_id,
                kind=ArtifactKind.TOOL_PLAN,
                payload={
                    "plan": plan.model_dump(mode="json"),
                    "risk_discrepancies": [d.model_dump(mode="json") for d in discrepancies],
                    # What the binding could not type-check. Stored in the
                    # tool-plan artifact and readable via `/plan`; no UI shows
                    # it yet — displaying it at approval is ticket T5a.
                    "types_not_checked": {
                        t.name: list(t.source.unchecked) for t in bound.tools if t.source.unchecked
                    },
                },
            )
        )
        session = await self._move(session, S.TOOL_PLAN_READY, actor, "Tool plan ready")
        session = await self._move(
            session, S.TOOL_PLAN_APPROVAL_PENDING, actor, "Waiting for the plan decision"
        )
        return StepResult(
            session,
            f"{len(plan.tools)} tool(s) designed.",
            GateRequest(
                ApprovalGate.TOOL_PLAN,
                artifact.hash,
                f"Approve the WebMCP tool plan: {', '.join(plan.tool_names())}",
            ),
        )

    async def generate(
        self, session_id: str, owner: str, actor: Actor, *, approval_id: str | None = None
    ) -> StepResult:
        """Agent 3: the approved plan becomes a patch. No model call.

        Entered from the plan gate (which needs the TOOL_PLAN approval), as a
        retry, or as the bounded regeneration after a failed review or
        validation.
        """
        session, _ = await self._load(session_id, owner)
        self._expect(
            session,
            S.TOOL_PLAN_APPROVAL_PENDING,
            S.GENERATION_RUNNING,
            S.SECURITY_REVIEW_FAILED,
            S.VALIDATION_FAILED,
        )
        plan, plan_artifact = await self._plan(session)

        if session.state is S.TOOL_PLAN_APPROVAL_PENDING:
            if approval_id is None:
                raise ApprovalRequiredError(S.TOOL_PLAN_APPROVED, ApprovalGate.TOOL_PLAN)
            await self._require_current(session, approval_id, S.TOOL_PLAN_APPROVAL_PENDING)
            session = await self._move(
                session,
                S.TOOL_PLAN_APPROVED,
                actor,
                "The developer approved the tool plan",
                approval_id=approval_id,
                artifact_hash=plan_artifact.hash,
            )
            session = await self._move(session, S.GENERATION_RUNNING, actor, "Generating")
        elif session.state in (S.SECURITY_REVIEW_FAILED, S.VALIDATION_FAILED):
            events = await self._store.list_events(session.id, owner)
            previous = sum(
                1
                for e in events
                if e.kind == "state.changed"
                and e.detail.get("to") == S.GENERATION_RUNNING.value
                and e.detail.get("from")
                in (S.SECURITY_REVIEW_FAILED.value, S.VALIDATION_FAILED.value)
            )
            try:
                await self._machine.record_failure_loop(
                    session, previous + 1, f"{session.state.value} routed back to generation"
                )
            except RetryLimitExceededError as exc:
                raise PipelineError(str(exc)) from exc
            session = await self._move(
                session, S.GENERATION_RUNNING, actor, f"Regenerating after {session.state.value}"
            )
        else:
            session = await self._move(session, S.GENERATION_RUNNING, actor, "Retrying generation")

        _, index, base = await self._analysis(session)
        try:
            _, patch = self._build(plan, index, base)
        except (ToolsetConversionError, GeneratedSecretError) as exc:
            raise await self._fail(session, "Generating the integration", exc) from exc

        await self._store.put_artifact(
            Artifact(
                session_id=session.id,
                project_id=session.project_id,
                kind=ArtifactKind.PATCH,
                payload=patch.hashable(),
            )
        )
        session = await self._move(session, S.PATCH_READY, actor, "Patch ready")
        return StepResult(session, patch.summary)

    async def review(self, session_id: str, owner: str, actor: Actor) -> StepResult:
        """Agent 4 reviews; `evaluate_gate` decides, pessimistically."""
        session, _ = await self._load(session_id, owner)
        self._expect(session, S.PATCH_READY)
        # Loaded before the transition: SECURITY_REVIEW_RUNNING has no retry
        # self-loop, so a failure after entering it would strand the run.
        _, patch, plan, patch_artifact = await self._approved_patch(session)
        session = await self._move(session, S.SECURITY_REVIEW_RUNNING, actor, "Security review")

        try:
            report, _ = await SecurityReviewer(self._gemini).run(
                SecurityReviewInput(plan=plan), self._trace(session, "Running security review")
            )
        except AgentError as exc:
            # No report means the gate cannot pass: the reviewer's input is one
            # of the two things it combines. Recorded as a failed review.
            await self._store.put_artifact(
                Artifact(
                    session_id=session.id,
                    project_id=session.project_id,
                    kind=ArtifactKind.SECURITY_REVIEW,
                    payload={"completed": False, "reason": str(exc)[:400]},
                )
            )
            session = await self._move(
                session, S.SECURITY_REVIEW_FAILED, actor, "The security review could not complete"
            )
            return StepResult(session, f"The security review could not complete: {exc}")

        verdict = evaluate_gate(report, plan, patch)
        await self._store.put_artifact(
            Artifact(
                session_id=session.id,
                project_id=session.project_id,
                kind=ArtifactKind.SECURITY_REVIEW,
                payload={"completed": True, "verdict": verdict.model_dump(mode="json")},
            )
        )
        if not verdict.passed:
            session = await self._move(session, S.SECURITY_REVIEW_FAILED, actor, verdict.reason)
            return StepResult(session, verdict.reason)

        session = await self._move(session, S.SECURITY_REVIEW_PASSED, actor, verdict.reason)
        session = await self._move(
            session, S.PATCH_APPROVAL_PENDING, actor, "Waiting for the patch decision"
        )
        return StepResult(
            session,
            verdict.reason,
            GateRequest(
                ApprovalGate.PATCH,
                patch_artifact.hash,
                f"Approve the generated patch: {len(patch.files)} file(s)",
            ),
        )

    async def validate(
        self, session_id: str, owner: str, actor: Actor, *, approval_id: str
    ) -> StepResult:
        """Agent 5: apply the approved patch in a no-network workspace and run it.

        The verdict comes from `orchestration/validation_run.py` — exit codes,
        computed by the validator in a module that cannot reach a model. Anything
        that stops the run before it produces evidence is a failed validation,
        not a pass by omission.
        """
        session, project = await self._load(session_id, owner)
        self._expect(session, S.PATCH_APPROVAL_PENDING)
        # Every precondition is checked before the first transition. Once the run
        # is in VALIDATION_RUNNING — which has no retry self-loop — its only way
        # out is a verdict, so nothing may fail between here and there.
        # Pinned by `test_a_validation_precondition_failing_never_strands_the_run`.
        executor = self._require_executor()
        toolset, patch, _, patch_artifact = await self._approved_patch(session)
        await self._require_current(session, approval_id, S.PATCH_APPROVAL_PENDING)
        session = await self._move(
            session,
            S.PATCH_APPROVED,
            actor,
            "The developer approved the patch",
            approval_id=approval_id,
            artifact_hash=patch_artifact.hash,
        )
        session = await self._move(session, S.VALIDATION_RUNNING, actor, "Validating")

        outcome: ValidationOutcome | None = None
        failure: str | None = None
        dependencies: dict[str, Any] | None = None
        label = "Validation could not run"
        try:
            outcome, dependencies = await self._validate_in_workspace(
                session, project, executor, toolset, patch
            )
        except CouldNotValidateError as exc:
            failure, dependencies, label = str(exc)[:400], exc.dependencies, "Could not validate"
        except Exception as exc:
            # Deliberately broad: whatever went wrong, the run is in
            # VALIDATION_RUNNING and must leave it as a failure with the reason
            # recorded — never stay there, and never read as a pass.
            failure = f"{type(exc).__name__}: {exc}"[:400]

        if outcome is None:
            await self._store.put_artifact(
                Artifact(
                    session_id=session.id,
                    project_id=session.project_id,
                    kind=ArtifactKind.VALIDATION,
                    payload={
                        "completed": False,
                        "validated": False,
                        "reason": failure,
                        "dependencies": dependencies,
                    },
                )
            )
            await self._event(session, "step.failed", label, reason=failure)
            session = await self._move(session, S.VALIDATION_FAILED, actor, f"{label}: {failure}")
            return StepResult(session, f"{label}: {failure}")

        await self._store.put_artifact(
            Artifact(
                session_id=session.id,
                project_id=session.project_id,
                kind=ArtifactKind.VALIDATION,
                payload={**outcome.payload, "dependencies": dependencies},
            )
        )
        if outcome.passed:
            session = await self._move(session, S.VALIDATION_PASSED, actor, outcome.summary)
        elif outcome.unexecuted:
            session = await self._move(
                session,
                S.VALIDATION_FAILED,
                actor,
                "Could not validate: required checks did not run: " + ", ".join(outcome.unexecuted),
            )
        else:
            failed = ", ".join(outcome.failed_check_ids) or "no check produced evidence"
            session = await self._move(
                session, S.VALIDATION_FAILED, actor, f"{outcome.summary}; failed: {failed}"
            )
        return StepResult(session, outcome.summary)

    async def _validate_in_workspace(
        self,
        session: Session,
        project: Project,
        executor: SecureExecutionProvider,
        toolset: WebMCPToolset,
        patch: GeneratedPatch,
    ) -> tuple[ValidationOutcome, dict[str, Any]]:
        """Create the workspace, give it dependencies, apply the patch, run agent 5.

        Returns the outcome and a label saying where the dependency tree came
        from. The validation workspace is created without the network, always;
        a connected repository's dependencies are installed in a separate,
        networked workspace by `install_dependencies` and copied in.
        """
        workspace = await executor.create_workspace(
            WorkspaceSpec(run_id=f"{session.id}-validation")
        )
        try:
            base = await self._materialise(project, executor, workspace)
            if base != patch.base_commit:
                raise PipelineError(
                    f"The repository is at {base}, but the patch was generated against "
                    f"{patch.base_commit}. Analyze again."
                )
            dependencies: dict[str, Any]
            if project.is_demo:
                linked = await anyio.to_thread.run_sync(self._link_dependencies, project, workspace)
                dependencies = {
                    "source": "demo-fixture" if linked else "none",
                    "detail": (
                        "The bundled demo fixture's dependency tree, installed in "
                        "MCPForge's own workspace by `npm ci`."
                        if linked
                        else "No dependency tree was available for the demo fixture."
                    ),
                    "install": None,
                }
            else:
                install = await install_dependencies(executor, workspace, app_dir=CHECKOUT_DIR)
                dependencies = {
                    "source": "install-step",
                    "detail": install.reason,
                    "install": install.as_payload(),
                }
                if not install.installed:
                    raise CouldNotValidateError(install.reason, dependencies)

            def apply() -> None:
                for change in patch.files:
                    target = resolve_inside(workspace, f"{CHECKOUT_DIR}/{change.path}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(change.contents, encoding="utf-8")

            await anyio.to_thread.run_sync(apply)
            await self._event(
                session, "step.progress", "Patch applied", dependency_source=dependencies["source"]
            )
            outcome = await validate_workspace(
                executor,
                workspace,
                toolset,
                app_dir=CHECKOUT_DIR,
                timeout_seconds=self._validation_timeout,
            )
            return outcome, dependencies
        finally:
            await executor.destroy(workspace)

    async def request_pull_request(self, session_id: str, owner: str, actor: Actor) -> StepResult:
        """Open the pull-request gate — only for a bound, elevated project.

        The writer's own boundary functions are applied **before** any
        transition. A demo project therefore stays at `VALIDATION_PASSED`: it is
        refused here, and `BranchAndPullRequestWriter` refuses it again if it is
        ever handed one (`03_SECURITY_ACCESS.md` §5).
        """
        session, project = await self._load(session_id, owner)
        self._expect(session, S.VALIDATION_PASSED)
        try:
            assert_within_boundary(project, project.repository_full_name or "")
            assert_may_write(project)
        except (BoundaryError, AccessModeError) as exc:
            raise PullRequestRefusedError(str(exc)) from exc

        _, _, _, patch_artifact = await self._approved_patch(session)
        session = await self._move(
            session, S.PR_APPROVAL_PENDING, actor, "Waiting for the pull-request decision"
        )
        return StepResult(
            session,
            "Waiting for the pull-request decision.",
            GateRequest(
                ApprovalGate.PULL_REQUEST,
                patch_artifact.hash,
                f"Open a pull request on {project.repository_full_name}",
            ),
        )

    async def create_pull_request(
        self, session_id: str, owner: str, actor: Actor, *, approval_id: str | None = None
    ) -> StepResult:
        """Hand the approved patch to `BranchAndPullRequestWriter`.

        The writer re-checks everything itself — boundary, `WRITE_PR`, both
        approvals by hash, branch shape, the base, and the policy engine — and
        is not routed around. It is given the base branch's head as GitHub
        reports it now, so its "the base moved" refusal compares real state.
        """
        session, project = await self._load(session_id, owner)
        self._expect(session, S.PR_APPROVAL_PENDING, S.PR_CREATING)
        _, patch, plan, patch_artifact = await self._approved_patch(session)

        if session.state is S.PR_APPROVAL_PENDING:
            if approval_id is None:
                raise ApprovalRequiredError(S.PR_APPROVED, ApprovalGate.PULL_REQUEST)
            await self._require_current(session, approval_id, S.PR_APPROVAL_PENDING)
            session = await self._move(
                session,
                S.PR_APPROVED,
                actor,
                "The developer approved the pull request",
                approval_id=approval_id,
                artifact_hash=patch_artifact.hash,
            )
            session = await self._move(session, S.PR_CREATING, actor, "Opening the pull request")
        else:
            session = await self._move(session, S.PR_CREATING, actor, "Retrying the pull request")

        pr_approval = await self._store.find_approval(
            session.id, ApprovalGate.PULL_REQUEST, patch_artifact.hash, owner
        )
        patch_approval = await self._store.find_approval(
            session.id, ApprovalGate.PATCH, patch_artifact.hash, owner
        )
        if project.repository_full_name is None:
            raise PullRequestRefusedError("The project has no bound repository.")
        repository, token = await self._installation_repository(project.repository_full_name)
        try:
            head = await self._github.get_branch_head(
                token, repository.full_name, project.base_branch or repository.default_branch
            )
        except GitHubError as exc:
            raise await self._fail(session, "Reading the base branch", exc) from exc

        writer = BranchAndPullRequestWriter(
            token=token,
            http=(
                httpx.AsyncClient(transport=self._writer_transport, timeout=60)
                if self._writer_transport is not None
                else None
            ),
        )
        try:
            outcome = await writer.create_pull_request(
                project=project,
                repository_full_name=repository.full_name,
                default_branch=repository.default_branch,
                base_commit=head,
                branch=branch_name_for(session.id),
                patch=patch,
                plan=plan,
                patch_approval=patch_approval,
                pr_approval=pr_approval,
                session_id=session.id,
            )
        except (WriteRefusedError, BoundaryError, AccessModeError) as exc:
            raise await self._fail(session, "Opening the pull request", exc) from exc
        finally:
            await writer.aclose()

        if not outcome.succeeded or not isinstance(outcome.pull_request, PullRequest):
            raise await self._fail(
                session, "Opening the pull request", PipelineError(outcome.explain())
            )
        pull = outcome.pull_request
        await self._event(
            session,
            "artifact.ready",
            "Pull request opened",
            url=pull.url,
            number=pull.number,
            branch=pull.branch,
        )
        session = await self._move(session, S.PR_CREATED, actor, f"Opened {pull.url}")
        session = await self._move(session, S.COMPLETE, actor, "Run complete")
        return StepResult(session, outcome.explain(), pull_request_url=pull.url)

    async def reject(
        self, session_id: str, owner: str, actor: Actor, *, approval_id: str
    ) -> StepResult:
        """Consume a REJECTED decision: return to the table's previous decision point.

        The rejection is a stored record decided by a human, for the gate the
        run is waiting at, over the artifact it shows. When the run returns to
        another gate, that gate is opened afresh — the earlier approval there
        does not carry it forward (`ApprovalNotCurrentError`).
        """
        session, _ = await self._load(session_id, owner)
        if session.state not in PENDING_GATES:
            raise StageStateError(
                f"The run is in {session.state.value}; there is nothing to reject."
            )
        gate, kind = PENDING_GATES[session.state]
        artifact = await self._artifact(session, kind)
        try:
            decision: Approval = await self._store.get_approval(approval_id, owner)
        except NotFoundError as exc:
            raise PipelineError("That decision does not exist.") from exc
        if (
            decision.session_id != session.id
            or decision.project_id != session.project_id
            or decision.gate is not gate
            or decision.status is not ApprovalStatus.REJECTED
            or decision.artifact_hash != artifact.hash
        ):
            raise PipelineError(
                f"That is not a rejection of the {gate.value} gate this run is waiting at."
            )
        await self._require_current(session, approval_id, session.state)

        target = rejection_target(session.state)
        session = await self._move(session, target, actor, f"The developer rejected {gate.value}")
        if target not in PENDING_GATES:
            return StepResult(session, f"{gate.value} rejected. Choose again.")

        reopened_gate, reopened_kind = PENDING_GATES[target]
        reopened = await self._artifact(session, reopened_kind)
        return StepResult(
            session,
            f"{gate.value} rejected.",
            GateRequest(reopened_gate, reopened.hash, f"Decide again: {reopened_gate.value}"),
        )
