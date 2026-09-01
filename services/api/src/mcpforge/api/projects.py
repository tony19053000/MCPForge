"""Project and session routes. Ownership is enforced on every request."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from mcpforge.api.deps import CurrentIdentity
from mcpforge.models.core import AccessMode, Project, RunState, Session
from mcpforge.store.port import NotFoundError, Store

router = APIRouter(prefix="/api", tags=["projects"])


def store_of(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProjectResponse(BaseModel):
    id: str
    name: str
    access_mode: AccessMode
    repository_full_name: str | None
    is_demo: bool

    @classmethod
    def of(cls, project: Project) -> ProjectResponse:
        return cls(
            id=project.id,
            name=project.name,
            access_mode=project.access_mode,
            repository_full_name=project.repository_full_name,
            is_demo=project.is_demo,
        )


class SessionResponse(BaseModel):
    id: str
    project_id: str
    title: str
    state: RunState


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(
    body: CreateProjectRequest, identity: CurrentIdentity, request: Request
) -> ProjectResponse:
    # Owner comes from the verified token, never from the request body.
    project = await store_of(request).create_project(
        Project(owner_uid=identity.subject, name=body.name)
    )
    return ProjectResponse.of(project)


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(identity: CurrentIdentity, request: Request) -> list[ProjectResponse]:
    projects = await store_of(request).list_projects(identity.subject)
    return [ProjectResponse.of(p) for p in projects]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str, identity: CurrentIdentity, request: Request
) -> ProjectResponse:
    try:
        project = await store_of(request).get_project(project_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from exc
    return ProjectResponse.of(project)


@router.post("/projects/{project_id}/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    project_id: str, identity: CurrentIdentity, request: Request
) -> SessionResponse:
    store = store_of(request)
    try:
        await store.get_project(project_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from exc

    session = await store.create_session(Session(project_id=project_id, owner_uid=identity.subject))
    return SessionResponse(
        id=session.id, project_id=session.project_id, title=session.title, state=session.state
    )
