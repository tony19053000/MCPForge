"""The run state machine — ticket F4-05, 02_ARCHITECTURE.md §6.

This is the module that turns `models/transitions.py` from data into
enforcement. Until now that table was described and tested but consulted by
nothing; the docstring said so plainly. This is the consulting code.

Three rules, all enforced here so no caller has to remember them:

1. An illegal transition raises. It does not warn and it does not proceed.
2. A gated state cannot be entered without an `Approval` that is APPROVED and
   whose `artifact_hash` matches the artifact being acted on.
3. Every transition is persisted with an actor and a cause.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcpforge.logging import get_logger
from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    Origin,
    RunEvent,
    RunState,
    Session,
)
from mcpforge.models.transitions import (
    AWAITING_HUMAN,
    ApprovalRequiredError,
    IllegalTransitionError,
    gate_for,
    is_legal,
)
from mcpforge.store.port import Store

log = get_logger(__name__)


class RetryLimitExceededError(Exception):
    """A failure loop ran out of attempts. The run halts and reports."""


@dataclass(frozen=True)
class TransitionRecord:
    """What happened, for the timeline and for audit."""

    from_state: RunState
    to_state: RunState
    actor: str
    origin: Origin
    cause: str


class RunMachine:
    """Guards every state change for one session."""

    #: How many times a failure may route back to generation before halting.
    max_failure_loops: int = 3

    def __init__(self, store: Store) -> None:
        self._store = store

    async def transition(
        self,
        session: Session,
        target: RunState,
        *,
        actor: str,
        origin: Origin,
        cause: str,
        approval: Approval | None = None,
        artifact_hash: str | None = None,
    ) -> Session:
        """Move a session to `target`, or raise.

        `approval` and `artifact_hash` are required when entering a gated state
        and ignored otherwise, so a caller cannot accidentally pass an approval
        for the wrong thing and have it quietly accepted.
        """
        current = session.state

        if not is_legal(current, target):
            raise IllegalTransitionError(current, target)

        gate = gate_for(target)
        if gate is not None:
            self._require_approval(gate, target, approval, artifact_hash)

        updated = session.model_copy(update={"state": target})
        await self._store.update_session(updated)

        await self._store.append_event(
            RunEvent(
                session_id=session.id,
                kind="state.changed",
                label=self._label(target),
                detail={
                    "from": current.value,
                    "to": target.value,
                    "actor": actor,
                    "cause": cause,
                },
                origin=origin,
            )
        )
        log.info(
            "run.transition",
            session_id=session.id,
            **{"from": current.value},
            to=target.value,
            actor=actor,
            origin=origin.value,
        )
        return updated

    @staticmethod
    def _require_approval(
        gate: ApprovalGate,
        target: RunState,
        approval: Approval | None,
        artifact_hash: str | None,
    ) -> None:
        """The gate check. Reads a stored record and nothing else."""
        if approval is None or artifact_hash is None:
            raise ApprovalRequiredError(target, gate)
        if approval.gate is not gate:
            raise ApprovalRequiredError(target, gate)
        # covers() checks status is APPROVED *and* the hash still matches, so a
        # regenerated artifact invalidates the approval automatically.
        if not approval.covers(artifact_hash):
            raise ApprovalRequiredError(target, gate)

    @staticmethod
    def _label(state: RunState) -> str:
        """Task-level label for the timeline. Never model reasoning."""
        return state.value.replace("_", " ").capitalize()

    def is_awaiting_human(self, session: Session) -> bool:
        return session.state in AWAITING_HUMAN

    async def record_failure_loop(self, session: Session, attempt: int, reason: str) -> None:
        """A security-review or validation failure routing back to generation.

        02_ARCHITECTURE.md §6 requires the loop be bounded and to halt loudly on
        exhaustion rather than spinning.
        """
        if attempt > self.max_failure_loops:
            await self._store.append_event(
                RunEvent(
                    session_id=session.id,
                    kind="step.failed",
                    label="Halted after repeated failures",
                    detail={"attempts": attempt, "reason": reason},
                    origin=Origin.SYSTEM,
                )
            )
            raise RetryLimitExceededError(
                f"Halting after {attempt - 1} regeneration attempts. Last reason: {reason}"
            )

        await self._store.append_event(
            RunEvent(
                session_id=session.id,
                kind="step.progress",
                label="Regenerating after findings",
                detail={"attempt": attempt, "reason": reason},
                origin=Origin.SYSTEM,
            )
        )
