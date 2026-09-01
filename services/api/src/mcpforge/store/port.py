"""The persistence port.

Adapters implement this. No adapter-specific type may leak past it, so the store
can move from in-memory to Firestore without touching anything above.

Ownership is enforced here, at the query level: every read takes an owner uid and
returns nothing for a project the caller does not own.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    Project,
    RunEvent,
    Session,
    Turn,
)


class NotFoundError(Exception):
    """The object does not exist, or the caller does not own it.

    Deliberately one error for both, so the store never reveals the existence of
    another user's project.
    """


@runtime_checkable
class Store(Protocol):
    # Projects
    async def create_project(self, project: Project) -> Project: ...
    async def get_project(self, project_id: str, owner_uid: str) -> Project: ...
    async def list_projects(self, owner_uid: str) -> list[Project]: ...
    async def update_project(self, project: Project) -> Project: ...

    # Sessions
    async def create_session(self, session: Session) -> Session: ...
    async def get_session(self, session_id: str, owner_uid: str) -> Session: ...
    async def list_sessions(self, project_id: str, owner_uid: str) -> list[Session]: ...
    async def update_session(self, session: Session) -> Session: ...

    # Conversation
    async def append_turn(self, turn: Turn) -> Turn: ...
    async def list_turns(self, session_id: str, owner_uid: str) -> list[Turn]: ...

    # Activity
    async def append_event(self, event: RunEvent) -> RunEvent: ...
    async def list_events(self, session_id: str, owner_uid: str) -> list[RunEvent]: ...

    # Approvals
    async def create_approval(self, approval: Approval) -> Approval: ...
    async def get_approval(self, approval_id: str, owner_uid: str) -> Approval: ...
    async def update_approval(self, approval: Approval) -> Approval: ...
    async def find_approval(
        self, session_id: str, gate: ApprovalGate, artifact_hash: str, owner_uid: str
    ) -> Approval | None: ...
