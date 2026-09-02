"""The run state machine — F4-05.

The transition table was data until now. This is the code that consults it, so
these tests are what make "unreachable without an approval" a fact rather than
an intention.
"""

from __future__ import annotations

import pytest

from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    ApprovalStatus,
    Origin,
    Project,
    RunState,
    Session,
    artifact_hash,
    utcnow,
)
from mcpforge.models.transitions import (
    GATED_TRANSITIONS,
    LEGAL,
    ApprovalRequiredError,
    IllegalTransitionError,
)
from mcpforge.orchestration.machine import RetryLimitExceededError, RunMachine
from mcpforge.store.memory import InMemoryStore

OWNER = "uid-owner"
PLAN_HASH = artifact_hash({"tools": ["search_rooms", "cancel_reservation"]})


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def machine(store: InMemoryStore) -> RunMachine:
    return RunMachine(store)


async def make_session(store: InMemoryStore, state: RunState) -> Session:
    """A session in its own new project."""
    project = await store.create_project(Project(owner_uid=OWNER, name="hotel"))
    return await make_session_in(store, project, state)


async def make_session_in(store: InMemoryStore, project: Project, state: RunState) -> Session:
    """A session in an existing project.

    Needed because `make_session` creates a fresh project each time, which meant
    a test intended to cover 'another session' was actually only covering
    'another project' — and the project check alone would have passed it.
    """
    return await store.create_session(
        Session(project_id=project.id, owner_uid=project.owner_uid, state=state)
    )


async def store_approval(
    store: InMemoryStore,
    session: Session,
    gate: ApprovalGate,
    hash_: str = PLAN_HASH,
    *,
    status: ApprovalStatus = ApprovalStatus.APPROVED,
) -> Approval:
    """Persist a real approval for this session, as the API would."""
    return await store.create_approval(
        Approval(
            project_id=session.project_id,
            session_id=session.id,
            gate=gate,
            artifact_hash=hash_,
            summary="x",
            status=status,
            actor_uid=OWNER if status is not ApprovalStatus.PENDING else None,
            decided_at=utcnow() if status is not ApprovalStatus.PENDING else None,
        )
    )


# -- legality ---------------------------------------------------------------


async def test_a_legal_transition_moves_the_session(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.ANALYSIS_PENDING)
    updated = await machine.transition(
        session,
        RunState.ANALYSIS_RUNNING,
        actor="system",
        origin=Origin.SYSTEM,
        cause="analysis started",
    )
    assert updated.state is RunState.ANALYSIS_RUNNING
    assert (await store.get_session(session.id, OWNER)).state is RunState.ANALYSIS_RUNNING


async def test_an_illegal_transition_raises_rather_than_warning(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.PROJECT_CREATED)
    with pytest.raises(IllegalTransitionError):
        await machine.transition(
            session,
            RunState.PR_CREATED,
            actor="system",
            origin=Origin.SYSTEM,
            cause="skip everything",
        )
    assert (await store.get_session(session.id, OWNER)).state is RunState.PROJECT_CREATED


async def test_an_illegal_transition_leaves_no_event(
    store: InMemoryStore, machine: RunMachine
) -> None:
    """A refused transition must not appear in the timeline as if it happened."""
    session = await make_session(store, RunState.PROJECT_CREATED)
    with pytest.raises(IllegalTransitionError):
        await machine.transition(
            session, RunState.COMPLETE, actor="s", origin=Origin.SYSTEM, cause="nope"
        )
    assert await store.list_events(session.id, OWNER) == []


# -- gates ------------------------------------------------------------------


@pytest.mark.parametrize(("target", "gate"), list(GATED_TRANSITIONS.items()))
async def test_a_gated_state_cannot_be_entered_without_an_approval(
    store: InMemoryStore, machine: RunMachine, target: RunState, gate: ApprovalGate
) -> None:
    source = next(s for s, targets in LEGAL.items() if target in targets and s is not target)
    session = await make_session(store, source)
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(session, target, actor=OWNER, origin=Origin.HUMAN, cause="please")


async def test_a_matching_approval_opens_the_gate(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    approval = await store_approval(store, session, ApprovalGate.TOOL_PLAN)
    updated = await machine.transition(
        session,
        RunState.TOOL_PLAN_APPROVED,
        actor=OWNER,
        origin=Origin.HUMAN,
        cause="developer approved the plan",
        approval_id=approval.id,
        artifact_hash=PLAN_HASH,
    )
    assert updated.state is RunState.TOOL_PLAN_APPROVED


async def test_a_pending_approval_does_not_open_the_gate(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    pending = await store_approval(
        store, session, ApprovalGate.TOOL_PLAN, status=ApprovalStatus.PENDING
    )
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="x",
            approval_id=pending.id,
            artifact_hash=PLAN_HASH,
        )


async def test_a_rejected_approval_does_not_open_the_gate(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    rejected = await store_approval(
        store, session, ApprovalGate.TOOL_PLAN, status=ApprovalStatus.REJECTED
    )
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="x",
            approval_id=rejected.id,
            artifact_hash=PLAN_HASH,
        )


async def test_regenerating_the_artifact_closes_the_gate_again(
    store: InMemoryStore, machine: RunMachine
) -> None:
    """The reason approvals carry a hash: approving a plan is not approving a
    different plan."""
    session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    approval = await store_approval(store, session, ApprovalGate.TOOL_PLAN)
    changed = artifact_hash({"tools": ["search_rooms", "cancel_reservation", "delete_account"]})
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="x",
            approval_id=approval.id,
            artifact_hash=changed,
        )


async def test_an_approval_for_another_gate_does_not_open_this_one(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.PATCH_APPROVAL_PENDING)
    wrong_gate = await store_approval(store, session, ApprovalGate.TOOL_PLAN)
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.PATCH_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="x",
            approval_id=wrong_gate.id,
            artifact_hash=PLAN_HASH,
        )


# -- the recorded history ---------------------------------------------------


async def test_every_transition_is_persisted_with_actor_and_cause(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.ANALYSIS_PENDING)
    await machine.transition(
        session,
        RunState.ANALYSIS_RUNNING,
        actor=OWNER,
        origin=Origin.HUMAN,
        cause="developer asked for analysis",
    )
    events = await store.list_events(session.id, OWNER)
    assert len(events) == 1
    assert events[0].kind == "state.changed"
    assert events[0].detail["from"] == "ANALYSIS_PENDING"
    assert events[0].detail["to"] == "ANALYSIS_RUNNING"
    assert events[0].detail["actor"] == OWNER
    assert events[0].detail["cause"] == "developer asked for analysis"
    assert events[0].origin is Origin.HUMAN


async def test_an_agent_initiated_transition_is_labelled_as_such(
    store: InMemoryStore, machine: RunMachine
) -> None:
    """F7-04's requirement, enforced here: a human can always see what an agent
    did on their behalf."""
    session = await make_session(store, RunState.ANALYSIS_PENDING)
    await machine.transition(
        session,
        RunState.ANALYSIS_RUNNING,
        actor="agent:analyst",
        origin=Origin.AGENT,
        cause="agent invoked start_repository_analysis",
    )
    events = await store.list_events(session.id, OWNER)
    assert events[0].origin is Origin.AGENT


async def test_the_event_label_carries_no_reasoning(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.ANALYSIS_PENDING)
    await machine.transition(
        session, RunState.ANALYSIS_RUNNING, actor="s", origin=Origin.SYSTEM, cause="c"
    )
    events = await store.list_events(session.id, OWNER)
    assert events[0].label == "Analysis running"


# -- bounded failure loops --------------------------------------------------


async def test_a_failure_loop_is_bounded_and_halts_loudly(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.SECURITY_REVIEW_FAILED)
    for attempt in range(1, machine.max_failure_loops + 1):
        await machine.record_failure_loop(session, attempt, "overpowered tool")

    with pytest.raises(RetryLimitExceededError, match="Halting after"):
        await machine.record_failure_loop(
            session, machine.max_failure_loops + 1, "overpowered tool"
        )

    events = await store.list_events(session.id, OWNER)
    assert events[-1].kind == "step.failed"


async def test_awaiting_human_is_reported(store: InMemoryStore, machine: RunMachine) -> None:
    waiting = await make_session(store, RunState.PATCH_APPROVAL_PENDING)
    running = await make_session(store, RunState.GENERATION_RUNNING)
    assert machine.is_awaiting_human(waiting) is True
    assert machine.is_awaiting_human(running) is False


# -- the whole path ---------------------------------------------------------


async def test_the_full_pipeline_cannot_be_walked_without_three_approvals(
    store: InMemoryStore, machine: RunMachine
) -> None:
    """End to end: every gate demands its own approval for its own artifact."""
    patch_hash = artifact_hash({"files": ["src/webmcp/register.ts"]})
    pr_hash = artifact_hash({"branch": "mcpforge/webmcp-booking"})

    session = await make_session(store, RunState.PROJECT_CREATED)
    plain = [
        RunState.REPOSITORY_CONNECTED,
        RunState.ANALYSIS_PENDING,
        RunState.ANALYSIS_RUNNING,
        RunState.ANALYSIS_COMPLETE,
        RunState.WORKFLOW_SELECTION_PENDING,
        RunState.WORKFLOWS_SELECTED,
        RunState.TOOL_PLAN_RUNNING,
        RunState.TOOL_PLAN_READY,
        RunState.TOOL_PLAN_APPROVAL_PENDING,
    ]
    for target in plain:
        session = await machine.transition(
            session, target, actor="system", origin=Origin.SYSTEM, cause="step"
        )

    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session, RunState.TOOL_PLAN_APPROVED, actor=OWNER, origin=Origin.HUMAN, cause="try"
        )
    plan_approval = await store_approval(store, session, ApprovalGate.TOOL_PLAN, PLAN_HASH)
    session = await machine.transition(
        session,
        RunState.TOOL_PLAN_APPROVED,
        actor=OWNER,
        origin=Origin.HUMAN,
        cause="approved",
        approval_id=plan_approval.id,
        artifact_hash=PLAN_HASH,
    )

    for target in [
        RunState.GENERATION_RUNNING,
        RunState.PATCH_READY,
        RunState.SECURITY_REVIEW_RUNNING,
        RunState.SECURITY_REVIEW_PASSED,
        RunState.PATCH_APPROVAL_PENDING,
    ]:
        session = await machine.transition(
            session, target, actor="system", origin=Origin.SYSTEM, cause="step"
        )

    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session, RunState.PATCH_APPROVED, actor=OWNER, origin=Origin.HUMAN, cause="try"
        )
    patch_approval = await store_approval(store, session, ApprovalGate.PATCH, patch_hash)
    session = await machine.transition(
        session,
        RunState.PATCH_APPROVED,
        actor=OWNER,
        origin=Origin.HUMAN,
        cause="approved",
        approval_id=patch_approval.id,
        artifact_hash=patch_hash,
    )

    for target in [
        RunState.VALIDATION_RUNNING,
        RunState.VALIDATION_PASSED,
        RunState.PR_APPROVAL_PENDING,
    ]:
        session = await machine.transition(
            session, target, actor="system", origin=Origin.SYSTEM, cause="step"
        )

    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session, RunState.PR_APPROVED, actor=OWNER, origin=Origin.HUMAN, cause="try"
        )
    pr_approval = await store_approval(store, session, ApprovalGate.PULL_REQUEST, pr_hash)
    session = await machine.transition(
        session,
        RunState.PR_APPROVED,
        actor=OWNER,
        origin=Origin.HUMAN,
        cause="approved",
        approval_id=pr_approval.id,
        artifact_hash=pr_hash,
    )

    for target in [RunState.PR_CREATING, RunState.PR_CREATED, RunState.COMPLETE]:
        session = await machine.transition(
            session, target, actor="system", origin=Origin.SYSTEM, cause="step"
        )

    assert session.state is RunState.COMPLETE


# -- the gate must read the store, not the caller ---------------------------


async def test_an_approval_that_was_never_stored_does_not_open_a_gate(
    store: InMemoryStore, machine: RunMachine
) -> None:
    """An earlier version took an Approval object and trusted it.

    Anything that could call transition() could construct one. The record now
    comes from the store, so an id that is not in it opens nothing.
    """
    session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="fabricated",
            approval_id="appr_never_persisted",
            artifact_hash=PLAN_HASH,
        )
    assert (await store.get_session(session.id, OWNER)).state is RunState.TOOL_PLAN_APPROVAL_PENDING


async def test_an_approval_from_another_session_of_the_same_project_is_refused(
    store: InMemoryStore, machine: RunMachine
) -> None:
    """Two runs over the same repository produce the same content-derived hash.

    Same owner, same project, same gate, genuinely approved — and still refused,
    because it belongs to a different session. An earlier version of this test
    put the two sessions in different projects, so the project check passed it
    and the session check was never exercised.
    """
    project = await store.create_project(Project(owner_uid=OWNER, name="hotel"))
    victim = await make_session_in(store, project, RunState.TOOL_PLAN_APPROVAL_PENDING)
    other = await make_session_in(store, project, RunState.TOOL_PLAN_APPROVAL_PENDING)
    assert victim.project_id == other.project_id
    assert victim.id != other.id

    foreign = await store_approval(store, other, ApprovalGate.TOOL_PLAN, PLAN_HASH)

    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            victim,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="reused approval",
            approval_id=foreign.id,
            artifact_hash=PLAN_HASH,
        )


async def test_an_approval_from_another_project_is_refused(
    store: InMemoryStore, machine: RunMachine
) -> None:
    """The other half of the binding, pinned separately so neither check can be
    removed while the other covers for it."""
    victim = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    other_project_session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    assert victim.project_id != other_project_session.project_id

    foreign = await store_approval(store, other_project_session, ApprovalGate.TOOL_PLAN, PLAN_HASH)
    # Make the session ids match so only the project check can refuse it.
    foreign = await store.update_approval(
        foreign.model_copy(update={"session_id": victim.id}), OWNER
    )

    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            victim,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="cross-project approval",
            approval_id=foreign.id,
            artifact_hash=PLAN_HASH,
        )


async def test_an_approval_belonging_to_another_user_is_not_visible(
    store: InMemoryStore, machine: RunMachine
) -> None:
    """Loading goes through the store's ownership check, so a stranger's
    approval is not merely rejected — it cannot be read at all."""
    stranger_project = await store.create_project(Project(owner_uid="uid-stranger", name="x"))
    stranger_session = await store.create_session(
        Session(project_id=stranger_project.id, owner_uid="uid-stranger")
    )
    stranger_approval = await store_approval(
        store, stranger_session, ApprovalGate.TOOL_PLAN, PLAN_HASH
    )

    session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="someone else's approval",
            approval_id=stranger_approval.id,
            artifact_hash=PLAN_HASH,
        )


def test_transition_takes_an_approval_id_not_an_approval_object() -> None:
    """Structural: there is no parameter through which a caller could hand in a
    record it made up."""
    import inspect

    params = inspect.signature(RunMachine.transition).parameters
    assert "approval_id" in params
    assert "approval" not in params
    assert params["approval_id"].annotation in ("str | None", str | None)
