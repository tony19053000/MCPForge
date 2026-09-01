"""Firestore store adapter — ticket F3-08.

Uses Application Default Credentials, so there is no service-account key here
either (03_SECURITY_ACCESS.md §9).

Ownership is enforced **in the query**, not by filtering after the fetch. A
`where("owner_uid", "==", uid)` clause means a mis-scoped read returns nothing
rather than returning another user's document and relying on us to drop it.

No Firestore type crosses the port: everything in and out is a Pydantic model.
"""

from __future__ import annotations

from typing import Any

from google.cloud import firestore
from pydantic import BaseModel

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

PROJECTS = "projects"
SESSIONS = "sessions"
TURNS = "turns"
EVENTS = "events"
APPROVALS = "approvals"


class FirestoreStore:
    """Persistent store. Behaviour is pinned by the shared conformance suite."""

    def __init__(self, project_id: str, *, client: Any = None) -> None:
        # Credentials come from ADC. Nothing secret is passed in.
        self._db = client or firestore.AsyncClient(project=project_id)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_dict(model: BaseModel) -> dict[str, Any]:
        data: dict[str, Any] = model.model_dump(mode="json")
        return data

    async def _owned_project(self, project_id: str, owner_uid: str) -> Project:
        snapshot = await self._db.collection(PROJECTS).document(project_id).get()
        data = snapshot.to_dict() if snapshot.exists else None
        # Missing and forbidden are the same error, so the store never reveals
        # that another user's project exists.
        if data is None or data.get("owner_uid") != owner_uid:
            raise NotFoundError(f"project {project_id}")
        return Project.model_validate(data)

    async def _owned_session(self, session_id: str, owner_uid: str) -> Session:
        snapshot = await self._db.collection(SESSIONS).document(session_id).get()
        data = snapshot.to_dict() if snapshot.exists else None
        if data is None or data.get("owner_uid") != owner_uid:
            raise NotFoundError(f"session {session_id}")
        return Session.model_validate(data)

    # -- projects ----------------------------------------------------------

    async def create_project(self, project: Project) -> Project:
        await self._db.collection(PROJECTS).document(project.id).set(self._to_dict(project))
        return project

    async def get_project(self, project_id: str, owner_uid: str) -> Project:
        return await self._owned_project(project_id, owner_uid)

    async def list_projects(self, owner_uid: str) -> list[Project]:
        query = self._db.collection(PROJECTS).where(
            filter=firestore.FieldFilter("owner_uid", "==", owner_uid)
        )
        return [Project.model_validate(doc.to_dict()) async for doc in query.stream()]

    async def update_project(self, project: Project) -> Project:
        await self._owned_project(project.id, project.owner_uid)
        await self._db.collection(PROJECTS).document(project.id).set(self._to_dict(project))
        return project

    # -- sessions ----------------------------------------------------------

    async def create_session(self, session: Session) -> Session:
        await self._db.collection(SESSIONS).document(session.id).set(self._to_dict(session))
        return session

    async def get_session(self, session_id: str, owner_uid: str) -> Session:
        return await self._owned_session(session_id, owner_uid)

    async def list_sessions(self, project_id: str, owner_uid: str) -> list[Session]:
        await self._owned_project(project_id, owner_uid)
        query = (
            self._db.collection(SESSIONS)
            .where(filter=firestore.FieldFilter("project_id", "==", project_id))
            .where(filter=firestore.FieldFilter("owner_uid", "==", owner_uid))
        )
        return [Session.model_validate(doc.to_dict()) async for doc in query.stream()]

    async def update_session(self, session: Session) -> Session:
        await self._owned_session(session.id, session.owner_uid)
        await self._db.collection(SESSIONS).document(session.id).set(self._to_dict(session))
        return session

    # -- conversation ------------------------------------------------------

    async def append_turn(self, turn: Turn) -> Turn:
        await self._db.collection(TURNS).document(turn.id).set(self._to_dict(turn))
        return turn

    async def list_turns(self, session_id: str, owner_uid: str) -> list[Turn]:
        await self._owned_session(session_id, owner_uid)
        query = self._db.collection(TURNS).where(
            filter=firestore.FieldFilter("session_id", "==", session_id)
        )
        turns = [Turn.model_validate(doc.to_dict()) async for doc in query.stream()]
        # Ordered here rather than in the query, so no composite index is needed.
        return sorted(turns, key=lambda t: t.created_at)

    # -- activity ----------------------------------------------------------

    async def append_event(self, event: RunEvent) -> RunEvent:
        await self._db.collection(EVENTS).document(event.id).set(self._to_dict(event))
        return event

    async def list_events(self, session_id: str, owner_uid: str) -> list[RunEvent]:
        await self._owned_session(session_id, owner_uid)
        query = self._db.collection(EVENTS).where(
            filter=firestore.FieldFilter("session_id", "==", session_id)
        )
        events = [RunEvent.model_validate(doc.to_dict()) async for doc in query.stream()]
        return sorted(events, key=lambda e: e.created_at)

    # -- approvals ---------------------------------------------------------

    async def create_approval(self, approval: Approval) -> Approval:
        await self._db.collection(APPROVALS).document(approval.id).set(self._to_dict(approval))
        return approval

    async def get_approval(self, approval_id: str, owner_uid: str) -> Approval:
        snapshot = await self._db.collection(APPROVALS).document(approval_id).get()
        data = snapshot.to_dict() if snapshot.exists else None
        if data is None:
            raise NotFoundError(f"approval {approval_id}")
        approval = Approval.model_validate(data)
        # Ownership lives on the session, so it is checked there.
        await self._owned_session(approval.session_id, owner_uid)
        return approval

    async def update_approval(self, approval: Approval, owner_uid: str) -> Approval:
        await self._owned_session(approval.session_id, owner_uid)
        snapshot = await self._db.collection(APPROVALS).document(approval.id).get()
        if not snapshot.exists:
            raise NotFoundError(f"approval {approval.id}")
        await self._db.collection(APPROVALS).document(approval.id).set(self._to_dict(approval))
        return approval

    async def find_approval(
        self, session_id: str, gate: ApprovalGate, artifact_hash: str, owner_uid: str
    ) -> Approval | None:
        await self._owned_session(session_id, owner_uid)
        query = (
            self._db.collection(APPROVALS)
            .where(filter=firestore.FieldFilter("session_id", "==", session_id))
            .where(filter=firestore.FieldFilter("gate", "==", gate.value))
            .where(filter=firestore.FieldFilter("artifact_hash", "==", artifact_hash))
            .where(filter=firestore.FieldFilter("status", "==", ApprovalStatus.APPROVED.value))
        )
        async for doc in query.stream():
            return Approval.model_validate(doc.to_dict())
        return None
