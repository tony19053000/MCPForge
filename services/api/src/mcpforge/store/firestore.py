"""Firestore store adapter — ticket F3-08.

Uses Application Default Credentials, so there is no service-account key here
either (03_SECURITY_ACCESS.md §9).

Ownership is enforced **in the query**, not by filtering after the fetch. A
`where("owner_uid", "==", uid)` clause means a mis-scoped read returns nothing
rather than returning another user's document and relying on us to drop it.

No Firestore type crosses the port: everything in and out is a Pydantic model.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any

from google.auth.exceptions import DefaultCredentialsError
from google.cloud import firestore
from pydantic import BaseModel

from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    ApprovalStatus,
    Artifact,
    ArtifactKind,
    Project,
    RunEvent,
    Session,
    Turn,
)
from mcpforge.store.port import NotFoundError, StoreUnavailableError

PROJECTS = "projects"
SESSIONS = "sessions"
TURNS = "turns"
EVENTS = "events"
APPROVALS = "approvals"
ARTIFACTS = "artifacts"

#: Storage-only field on turns and events: the order they were written in.
#: Firestore streams documents in id order and ids are random, so two entries
#: sharing a `created_at` microsecond would otherwise come back in either order
#: — the in-memory adapter keeps insertion order. Stripped before validation.
WRITE_ORDER = "_write_order"

#: A document id that is never written. Reading it is one cheap read that
#: proves credentials, project, database and read permission all work.
PROBE_DOCUMENT = "mcpforge-startup-probe"
PROBE_TIMEOUT_SECONDS = 10.0

_sequence = itertools.count()


def _write_order() -> list[int]:
    # Nanoseconds first, so order holds across processes; the counter breaks
    # ties within one. A two-element list, not a tuple: Firestore stores arrays.
    return [time.time_ns(), next(_sequence)]


class FirestoreStore:
    """Persistent store. Behaviour is pinned by the shared conformance suite."""

    def __init__(self, project_id: str, *, client: Any = None) -> None:
        # Credentials come from ADC. Nothing secret is passed in.
        if client is not None:
            self._db = client
            return
        try:
            self._db = firestore.AsyncClient(project=project_id)
        except DefaultCredentialsError as exc:
            raise StoreUnavailableError(
                "STORE=firestore needs Application Default Credentials. Run "
                "`gcloud auth application-default login` in development; production "
                "uses the deployment's workload identity. Key files are unsupported."
            ) from exc

    async def check_reachable(self) -> None:
        """Fail loudly if the database cannot be read. Called once at startup."""
        try:
            await asyncio.wait_for(
                self._db.collection(PROJECTS).document(PROBE_DOCUMENT).get(),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # Deliberately broad: whatever the cause (auth, missing database,
            # permission, network, timeout), the service must not come up
            # believing it persists state when it cannot.
            raise StoreUnavailableError(
                f"Firestore is unreachable: {type(exc).__name__}: {str(exc)[:300]}"
            ) from exc

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _to_dict(model: BaseModel) -> dict[str, Any]:
        data: dict[str, Any] = model.model_dump(mode="json")
        return data

    @staticmethod
    def _ordered(raw: list[dict[str, Any] | None]) -> list[dict[str, Any]]:
        """Sort by write order and strip it. Documents written before the field
        existed sort first, by `created_at`."""
        docs = [d for d in raw if d is not None]
        docs.sort(key=lambda d: (d.get(WRITE_ORDER) or [0, 0], d.get("created_at", "")))
        for doc in docs:
            doc.pop(WRITE_ORDER, None)
        return docs

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
        data = {**self._to_dict(turn), WRITE_ORDER: _write_order()}
        await self._db.collection(TURNS).document(turn.id).set(data)
        return turn

    async def list_turns(self, session_id: str, owner_uid: str) -> list[Turn]:
        await self._owned_session(session_id, owner_uid)
        query = self._db.collection(TURNS).where(
            filter=firestore.FieldFilter("session_id", "==", session_id)
        )
        # Ordered here rather than in the query, so no composite index is needed.
        docs = self._ordered([doc.to_dict() async for doc in query.stream()])
        return [Turn.model_validate(doc) for doc in docs]

    # -- activity ----------------------------------------------------------

    async def append_event(self, event: RunEvent) -> RunEvent:
        data = {**self._to_dict(event), WRITE_ORDER: _write_order()}
        await self._db.collection(EVENTS).document(event.id).set(data)
        return event

    async def list_events(self, session_id: str, owner_uid: str) -> list[RunEvent]:
        await self._owned_session(session_id, owner_uid)
        query = self._db.collection(EVENTS).where(
            filter=firestore.FieldFilter("session_id", "==", session_id)
        )
        docs = self._ordered([doc.to_dict() async for doc in query.stream()])
        return [RunEvent.model_validate(doc) for doc in docs]

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

    # -- artifacts ---------------------------------------------------------

    def _artifact_id(self, session_id: str, kind: ArtifactKind) -> str:
        """Deterministic id, so writing again replaces rather than accumulates.

        That replacement is what invalidates an approval bound to the previous
        hash — the same behaviour the in-memory adapter has.
        """
        return f"{session_id}__{kind.value}"

    async def put_artifact(self, artifact: Artifact) -> Artifact:
        doc = self._artifact_id(artifact.session_id, artifact.kind)
        await self._db.collection(ARTIFACTS).document(doc).set(self._to_dict(artifact))
        return artifact

    async def get_artifact(
        self, session_id: str, kind: ArtifactKind, owner_uid: str
    ) -> Artifact | None:
        await self._owned_session(session_id, owner_uid)
        snapshot = (
            await self._db.collection(ARTIFACTS).document(self._artifact_id(session_id, kind)).get()
        )
        data = snapshot.to_dict() if snapshot.exists else None
        return Artifact.model_validate(data) if data is not None else None
