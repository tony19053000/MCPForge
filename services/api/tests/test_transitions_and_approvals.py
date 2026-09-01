"""State machine and approval binding — the security core.

These tests protect the two rules that everything else rests on:
an illegal transition raises, and an approval covers exactly the artifact it was
given for. See 03_SECURITY_ACCESS.md §7.
"""

from __future__ import annotations

import itertools

import pytest

from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    ApprovalStatus,
    RunState,
    artifact_hash,
    utcnow,
)
from mcpforge.models.transitions import (
    AWAITING_HUMAN,
    GATED_TRANSITIONS,
    LEGAL,
    gate_for,
    is_legal,
)

S = RunState


def approved(hash_: str, gate: ApprovalGate = ApprovalGate.TOOL_PLAN) -> Approval:
    return Approval(
        project_id="p",
        session_id="s",
        gate=gate,
        artifact_hash=hash_,
        summary="x",
        status=ApprovalStatus.APPROVED,
        actor_uid="uid-owner",
        decided_at=utcnow(),
    )


# -- the transition table --------------------------------------------------


def test_every_state_appears_in_the_table() -> None:
    assert set(LEGAL) == set(RunState)


def test_every_target_is_a_real_state() -> None:
    for targets in LEGAL.values():
        for target in targets:
            assert target in RunState


def test_the_happy_path_is_walkable_end_to_end() -> None:
    path = [
        S.PROJECT_CREATED,
        S.REPOSITORY_CONNECTED,
        S.ANALYSIS_PENDING,
        S.ANALYSIS_RUNNING,
        S.ANALYSIS_COMPLETE,
        S.WORKFLOW_SELECTION_PENDING,
        S.WORKFLOWS_SELECTED,
        S.TOOL_PLAN_RUNNING,
        S.TOOL_PLAN_READY,
        S.TOOL_PLAN_APPROVAL_PENDING,
        S.TOOL_PLAN_APPROVED,
        S.GENERATION_RUNNING,
        S.PATCH_READY,
        S.SECURITY_REVIEW_RUNNING,
        S.SECURITY_REVIEW_PASSED,
        S.PATCH_APPROVAL_PENDING,
        S.PATCH_APPROVED,
        S.VALIDATION_RUNNING,
        S.VALIDATION_PASSED,
        S.PR_APPROVAL_PENDING,
        S.PR_APPROVED,
        S.PR_CREATING,
        S.PR_CREATED,
        S.COMPLETE,
    ]
    for current, target in itertools.pairwise(path):
        assert is_legal(current, target), f"{current} -> {target} should be legal"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # The whole point of the machine: no skipping gates.
        (S.PROJECT_CREATED, S.PR_CREATED),
        (S.ANALYSIS_COMPLETE, S.GENERATION_RUNNING),
        (S.TOOL_PLAN_READY, S.TOOL_PLAN_APPROVED),
        (S.PATCH_READY, S.PATCH_APPROVED),
        (S.SECURITY_REVIEW_FAILED, S.PATCH_APPROVAL_PENDING),
        (S.VALIDATION_FAILED, S.PR_APPROVAL_PENDING),
        (S.PR_APPROVAL_PENDING, S.PR_CREATED),
        (S.COMPLETE, S.ANALYSIS_PENDING),
    ],
)
def test_gate_skipping_transitions_are_illegal(current: RunState, target: RunState) -> None:
    assert not is_legal(current, target)


def test_a_failed_security_review_cannot_reach_approval_without_regenerating() -> None:
    """The only way out of a failed review is back through generation."""
    assert LEGAL[S.SECURITY_REVIEW_FAILED] == frozenset({S.GENERATION_RUNNING})


def test_a_failed_validation_cannot_reach_a_pull_request() -> None:
    assert LEGAL[S.VALIDATION_FAILED] == frozenset({S.GENERATION_RUNNING})


def test_complete_is_terminal() -> None:
    assert LEGAL[S.COMPLETE] == frozenset()


def test_the_three_consequential_states_are_gated() -> None:
    assert GATED_TRANSITIONS == {
        S.TOOL_PLAN_APPROVED: ApprovalGate.TOOL_PLAN,
        S.PATCH_APPROVED: ApprovalGate.PATCH,
        S.PR_APPROVED: ApprovalGate.PULL_REQUEST,
    }


def test_every_approval_pending_state_is_marked_as_awaiting_a_human() -> None:
    pending = {s for s in RunState if s.value.endswith("_APPROVAL_PENDING")}
    assert pending <= AWAITING_HUMAN


def test_each_gated_state_is_only_reachable_from_its_pending_state() -> None:
    for gated in GATED_TRANSITIONS:
        sources = [s for s, targets in LEGAL.items() if gated in targets]
        assert len(sources) == 1, f"{gated} should have exactly one source, got {sources}"
        assert sources[0].value.endswith("_APPROVAL_PENDING")


# -- approval binding ------------------------------------------------------


def test_an_approval_covers_the_exact_artifact_it_was_given_for() -> None:
    plan = {"tools": ["search_hotels", "prepare_booking"]}
    approval = approved(artifact_hash(plan))
    assert approval.covers(artifact_hash(plan)) is True


def test_regenerating_the_artifact_invalidates_the_approval() -> None:
    """The reason approvals carry a hash: a changed plan is not an approved plan."""
    approval = approved(artifact_hash({"tools": ["search_hotels"]}))
    changed = artifact_hash({"tools": ["search_hotels", "cancel_reservation"]})
    assert approval.covers(changed) is False


def test_a_pending_approval_covers_nothing() -> None:
    plan_hash = artifact_hash({"tools": []})
    approval = approved(plan_hash)
    approval.status = ApprovalStatus.PENDING
    assert approval.covers(plan_hash) is False


def test_a_rejected_approval_covers_nothing() -> None:
    plan_hash = artifact_hash({"tools": []})
    approval = approved(plan_hash)
    approval.status = ApprovalStatus.REJECTED
    assert approval.covers(plan_hash) is False


def test_artifact_hash_is_stable_across_key_order() -> None:
    assert artifact_hash({"a": 1, "b": 2}) == artifact_hash({"b": 2, "a": 1})


def test_artifact_hash_changes_when_content_changes() -> None:
    assert artifact_hash({"risk": "READ"}) != artifact_hash({"risk": "DESTRUCTIVE"})


def test_gate_lookup_returns_none_for_ungated_states() -> None:
    assert gate_for(S.ANALYSIS_RUNNING) is None
    assert gate_for(S.PATCH_APPROVED) is ApprovalGate.PATCH


def test_a_retry_never_re_enters_an_approved_state() -> None:
    """Retries self-loop on the running step.

    An earlier version of the table let GENERATION_RUNNING fall back to
    TOOL_PLAN_APPROVED, giving a gated state a second entrance. Single-entrance
    is what makes "you cannot reach this state without an approval" checkable.
    """
    for state, targets in LEGAL.items():
        for target in targets:
            if target in GATED_TRANSITIONS and state is not target:
                assert state.value.endswith("_APPROVAL_PENDING"), (
                    f"{state} must not be able to enter the gated state {target}"
                )
