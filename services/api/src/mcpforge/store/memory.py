"""In-memory store adapter.

Used by tests and local development. Passes the same conformance suite as the
Firestore adapter, so behaviour cannot drift between them.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy

from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    ApprovalStatus,
    Project,
    RunEvent,
    Session,
    Turn,
)
from mcpforge.store.port import NotFoundError


class InMemoryStore:
    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}
        self._sessions: dict[str, Session] = {}
        self._turns: list[Turn] = []
        self._events: list[RunEvent] = []
        self._approvals: dict[str, Approval] = {}
        self._lock = asyncio.Lock()

    # -- ownership ---------------------------------------------------------

    async def _owned_project(self, project_id: str, owner_uid: str) -> Project:
        project = self._projects.get(project_id)
        if project is None or project.owner_uid != owner_uid:
            raise NotFoundError(f"project {project_id}")
        return project

    async def _owned_session(self, session_id: str, owner_uid: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None or session.owner_uid != owner_uid:
            raise NotFoundError(f"session {session_id}")
        return session

    # -- projects ----------------------------------------------------------

    async def create_project(self, project: Project) -> Project:
        async with self._lock:
            self._projects[project.id] = deepcopy(project)
        return project

    async def get_project(self, project_id: str, owner_uid: str) -> Project:
        return deepcopy(await self._owned_project(project_id, owner_uid))

    async def list_projects(self, owner_uid: str) -> list[Project]:
        return [deepcopy(p) for p in self._projects.values() if p.owner_uid == owner_uid]

    async def update_project(self, project: Project) -> Project:
        async with self._lock:
            await self._owned_project(project.id, project.owner_uid)
            self._projects[project.id] = deepcopy(project)
        return project

    # -- sessions ----------------------------------------------------------

    async def create_session(self, session: Session) -> Session:
        async with self._lock:
            self._sessions[session.id] = deepcopy(session)
        return session

    async def get_session(self, session_id: str, owner_uid: str) -> Session:
        return deepcopy(await self._owned_session(session_id, owner_uid))

    async def list_sessions(self, project_id: str, owner_uid: str) -> list[Session]:
        await self._owned_project(project_id, owner_uid)
        return [
            deepcopy(s)
            for s in self._sessions.values()
            if s.project_id == project_id and s.owner_uid == owner_uid
        ]

    async def update_session(self, session: Session) -> Session:
        async with self._lock:
            await self._owned_session(session.id, session.owner_uid)
            self._sessions[session.id] = deepcopy(session)
        return session

    # -- conversation ------------------------------------------------------

    async def append_turn(self, turn: Turn) -> Turn:
        async with self._lock:
            self._turns.append(deepcopy(turn))
        return turn

    async def list_turns(self, session_id: str, owner_uid: str) -> list[Turn]:
        await self._owned_session(session_id, owner_uid)
        return [deepcopy(t) for t in self._turns if t.session_id == session_id]

    # -- activity ----------------------------------------------------------

    async def append_event(self, event: RunEvent) -> RunEvent:
        async with self._lock:
            self._events.append(deepcopy(event))
        return event

    async def list_events(self, session_id: str, owner_uid: str) -> list[RunEvent]:
        await self._owned_session(session_id, owner_uid)
        return [deepcopy(e) for e in self._events if e.session_id == session_id]

    # -- approvals ---------------------------------------------------------

    async def create_approval(self, approval: Approval) -> Approval:
        async with self._lock:
            self._approvals[approval.id] = deepcopy(approval)
        return approval

    async def get_approval(self, approval_id: str, owner_uid: str) -> Approval:
        approval = self._approvals.get(approval_id)
        if approval is None:
            raise NotFoundError(f"approval {approval_id}")
        await self._owned_session(approval.session_id, owner_uid)
        return deepcopy(approval)

    async def update_approval(self, approval: Approval, owner_uid: str) -> Approval:
        # Ownership is re-checked on write, not only on the preceding read, so
        # the port's guarantee holds for any caller.
        await self._owned_session(approval.session_id, owner_uid)
        async with self._lock:
            if approval.id not in self._approvals:
                raise NotFoundError(f"approval {approval.id}")
            self._approvals[approval.id] = deepcopy(approval)
        return approval

    async def find_approval(
        self, session_id: str, gate: ApprovalGate, artifact_hash: str, owner_uid: str
    ) -> Approval | None:
        await self._owned_session(session_id, owner_uid)
        for approval in self._approvals.values():
            if (
                approval.session_id == session_id
                and approval.gate is gate
                and approval.artifact_hash == artifact_hash
                and approval.status is ApprovalStatus.APPROVED
            ):
                return deepcopy(approval)
        return None
