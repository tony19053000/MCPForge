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
    project = await store.create_project(Project(owner_uid=OWNER, name="hotel"))
    return await store.create_session(Session(project_id=project.id, owner_uid=OWNER, state=state))


def approved(gate: ApprovalGate, hash_: str = PLAN_HASH) -> Approval:
    return Approval(
        project_id="p",
        session_id="s",
        gate=gate,
        artifact_hash=hash_,
        summary="x",
        status=ApprovalStatus.APPROVED,
        actor_uid=OWNER,
        decided_at=utcnow(),
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
    updated = await machine.transition(
        session,
        RunState.TOOL_PLAN_APPROVED,
        actor=OWNER,
        origin=Origin.HUMAN,
        cause="developer approved the plan",
        approval=approved(ApprovalGate.TOOL_PLAN),
        artifact_hash=PLAN_HASH,
    )
    assert updated.state is RunState.TOOL_PLAN_APPROVED


async def test_a_pending_approval_does_not_open_the_gate(
    store: InMemoryStore, machine: RunMachine
) -> None:
    pending = approved(ApprovalGate.TOOL_PLAN).model_copy(update={"status": ApprovalStatus.PENDING})
    session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="x",
            approval=pending,
            artifact_hash=PLAN_HASH,
        )


async def test_a_rejected_approval_does_not_open_the_gate(
    store: InMemoryStore, machine: RunMachine
) -> None:
    rejected = approved(ApprovalGate.TOOL_PLAN).model_copy(
        update={"status": ApprovalStatus.REJECTED}
    )
    session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="x",
            approval=rejected,
            artifact_hash=PLAN_HASH,
        )


async def test_regenerating_the_artifact_closes_the_gate_again(
    store: InMemoryStore, machine: RunMachine
) -> None:
    """The reason approvals carry a hash: approving a plan is not approving a
    different plan."""
    session = await make_session(store, RunState.TOOL_PLAN_APPROVAL_PENDING)
    changed = artifact_hash({"tools": ["search_rooms", "cancel_reservation", "delete_account"]})
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.TOOL_PLAN_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="x",
            approval=approved(ApprovalGate.TOOL_PLAN),
            artifact_hash=changed,
        )


async def test_an_approval_for_another_gate_does_not_open_this_one(
    store: InMemoryStore, machine: RunMachine
) -> None:
    session = await make_session(store, RunState.PATCH_APPROVAL_PENDING)
    with pytest.raises(ApprovalRequiredError):
        await machine.transition(
            session,
            RunState.PATCH_APPROVED,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="x",
            approval=approved(ApprovalGate.TOOL_PLAN),
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

    gated: list[tuple[RunState, ApprovalGate, str]] = [
        (RunState.TOOL_PLAN_APPROVED, ApprovalGate.TOOL_PLAN, PLAN_HASH),
    ]
    for target, gate, hash_ in gated:
        with pytest.raises(ApprovalRequiredError):
            await machine.transition(session, target, actor=OWNER, origin=Origin.HUMAN, cause="try")
        session = await machine.transition(
            session,
            target,
            actor=OWNER,
            origin=Origin.HUMAN,
            cause="approved",
            approval=approved(gate, hash_),
            artifact_hash=hash_,
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
    session = await machine.transition(
        session,
        RunState.PATCH_APPROVED,
        actor=OWNER,
        origin=Origin.HUMAN,
        cause="approved",
        approval=approved(ApprovalGate.PATCH, patch_hash),
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
    session = await machine.transition(
        session,
        RunState.PR_APPROVED,
        actor=OWNER,
        origin=Origin.HUMAN,
        cause="approved",
        approval=approved(ApprovalGate.PULL_REQUEST, pr_hash),
        artifact_hash=pr_hash,
    )

    for target in [RunState.PR_CREATING, RunState.PR_CREATED, RunState.COMPLETE]:
        session = await machine.transition(
            session, target, actor="system", origin=Origin.SYSTEM, cause="step"
        )

    assert session.state is RunState.COMPLETE
