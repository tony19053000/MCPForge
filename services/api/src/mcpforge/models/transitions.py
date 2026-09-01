"""The legal state transition table — 02_ARCHITECTURE.md §6.

**Status: this module is data and vocabulary, not yet an enforcement point.**
The orchestrator that consults it lands in Phase 4 (ticket F4-05). Until then
`is_legal`, `gate_for`, `AWAITING_HUMAN`, `IllegalTransitionError` and
`ApprovalRequiredError` are defined and tested but called by no production code.
Do not describe them as enforced anywhere until F4-05 lands.

What the table encodes, and what F4-05 must enforce:

- Every transition is checked against `LEGAL`. An illegal transition raises; it
  does not warn.
- Entering a state in `GATED_TRANSITIONS` requires an `Approval` that is
  APPROVED and whose `artifact_hash` matches the artifact being acted on.
- Each gated state has exactly one entrance — its `*_APPROVAL_PENDING` state —
  which is what makes "unreachable without an approval" checkable.
- Retries self-loop on the running step rather than falling back into an
  already-approved state.
"""

from __future__ import annotations

from mcpforge.models.core import ApprovalGate, RunState

S = RunState

LEGAL: dict[RunState, frozenset[RunState]] = {
    S.PROJECT_CREATED: frozenset({S.REPOSITORY_CONNECTED, S.ANALYSIS_PENDING}),
    S.REPOSITORY_CONNECTED: frozenset({S.ANALYSIS_PENDING}),
    S.ANALYSIS_PENDING: frozenset({S.ANALYSIS_RUNNING}),
    # Retries self-loop. A running step never re-enters an already-approved
    # state, so every gated state keeps exactly one entrance: its pending state.
    S.ANALYSIS_RUNNING: frozenset({S.ANALYSIS_COMPLETE, S.ANALYSIS_RUNNING}),
    S.ANALYSIS_COMPLETE: frozenset({S.WORKFLOW_SELECTION_PENDING}),
    S.WORKFLOW_SELECTION_PENDING: frozenset({S.WORKFLOWS_SELECTED}),
    S.WORKFLOWS_SELECTED: frozenset({S.TOOL_PLAN_RUNNING}),
    S.TOOL_PLAN_RUNNING: frozenset({S.TOOL_PLAN_READY, S.TOOL_PLAN_RUNNING}),
    S.TOOL_PLAN_READY: frozenset({S.TOOL_PLAN_APPROVAL_PENDING}),
    S.TOOL_PLAN_APPROVAL_PENDING: frozenset({S.TOOL_PLAN_APPROVED, S.WORKFLOW_SELECTION_PENDING}),
    S.TOOL_PLAN_APPROVED: frozenset({S.GENERATION_RUNNING}),
    S.GENERATION_RUNNING: frozenset({S.PATCH_READY, S.GENERATION_RUNNING}),
    S.PATCH_READY: frozenset({S.SECURITY_REVIEW_RUNNING}),
    S.SECURITY_REVIEW_RUNNING: frozenset({S.SECURITY_REVIEW_PASSED, S.SECURITY_REVIEW_FAILED}),
    # A failed review routes back to generation with the findings as input.
    S.SECURITY_REVIEW_FAILED: frozenset({S.GENERATION_RUNNING}),
    S.SECURITY_REVIEW_PASSED: frozenset({S.PATCH_APPROVAL_PENDING}),
    S.PATCH_APPROVAL_PENDING: frozenset({S.PATCH_APPROVED, S.TOOL_PLAN_APPROVAL_PENDING}),
    S.PATCH_APPROVED: frozenset({S.VALIDATION_RUNNING}),
    S.VALIDATION_RUNNING: frozenset({S.VALIDATION_PASSED, S.VALIDATION_FAILED}),
    S.VALIDATION_FAILED: frozenset({S.GENERATION_RUNNING}),
    S.VALIDATION_PASSED: frozenset({S.PR_APPROVAL_PENDING}),
    S.PR_APPROVAL_PENDING: frozenset({S.PR_APPROVED, S.PATCH_APPROVAL_PENDING}),
    S.PR_APPROVED: frozenset({S.PR_CREATING}),
    S.PR_CREATING: frozenset({S.PR_CREATED, S.PR_CREATING}),
    S.PR_CREATED: frozenset({S.COMPLETE}),
    S.COMPLETE: frozenset(),
}

# Entering these states requires an approved Approval for the named gate.
GATED_TRANSITIONS: dict[RunState, ApprovalGate] = {
    S.TOOL_PLAN_APPROVED: ApprovalGate.TOOL_PLAN,
    S.PATCH_APPROVED: ApprovalGate.PATCH,
    S.PR_APPROVED: ApprovalGate.PULL_REQUEST,
}

# States where the run is waiting on a human and nothing may proceed.
AWAITING_HUMAN: frozenset[RunState] = frozenset(
    {
        S.WORKFLOW_SELECTION_PENDING,
        S.TOOL_PLAN_APPROVAL_PENDING,
        S.PATCH_APPROVAL_PENDING,
        S.PR_APPROVAL_PENDING,
    }
)


class IllegalTransitionError(Exception):
    def __init__(self, current: RunState, target: RunState) -> None:
        super().__init__(f"Illegal transition: {current.value} -> {target.value}")
        self.current = current
        self.target = target


class ApprovalRequiredError(Exception):
    """A gated transition was attempted without a matching approval."""

    def __init__(self, target: RunState, gate: ApprovalGate) -> None:
        super().__init__(
            f"Transition to {target.value} requires an approved {gate.value} approval "
            "matching the current artifact"
        )
        self.target = target
        self.gate = gate


def is_legal(current: RunState, target: RunState) -> bool:
    return target in LEGAL[current]


def gate_for(target: RunState) -> ApprovalGate | None:
    return GATED_TRANSITIONS.get(target)
