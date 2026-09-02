"""Repository selection, binding and access elevation — F3-02, F3-06.

Makes the boundary reachable from the running product. Every route here goes
through `mcpforge.github.boundary`, which is the single place the rules live.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from mcpforge.api.deps import CurrentIdentity
from mcpforge.github.boundary import (
    AccessModeError,
    BoundaryError,
    NoRepositoryBoundError,
    bind_repository,
    elevate_to_write,
    revoke_write,
)
from mcpforge.github.client import GitHubAppClient, GitHubError
from mcpforge.models.core import AccessMode, Project
from mcpforge.store.port import NotFoundError, Store

router = APIRouter(prefix="/api", tags=["repositories"])


def _store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


def _github(request: Request) -> GitHubAppClient:
    client: GitHubAppClient = request.app.state.github
    return client


class RepositoryDto(BaseModel):
    id: str
    full_name: str
    default_branch: str
    private: bool


class BindBody(BaseModel):
    repository_id: str = Field(min_length=1)
    full_name: str = Field(min_length=3)
    base_branch: str = Field(min_length=1)


class AccessDto(BaseModel):
    project_id: str
    access_mode: AccessMode
    repository_full_name: str | None
    base_branch: str | None
    elevated_by: str | None
    elevated_at: datetime | None


@router.get("/github/repositories", response_model=list[RepositoryDto])
async def list_available_repositories(
    identity: CurrentIdentity, request: Request
) -> list[RepositoryDto]:
    """Only what the App installation was granted. Never the whole account."""
    client = _github(request)
    if not client.configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub is not configured for this deployment"
        )
    try:
        installations = await client.list_installations()
        repositories: list[RepositoryDto] = []
        for installation in installations:
            token = await client.create_installation_token(installation.id)
            repositories.extend(
                RepositoryDto(
                    id=str(repo.id),
                    full_name=repo.full_name,
                    default_branch=repo.default_branch,
                    private=repo.private,
                )
                for repo in await client.list_repositories(token)
            )
    except GitHubError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return repositories


@router.post("/projects/{project_id}/repository", response_model=AccessDto)
async def bind_project_repository(
    project_id: str, body: BindBody, identity: CurrentIdentity, request: Request
) -> AccessDto:
    store = _store(request)
    try:
        project = await store.get_project(project_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from exc

    try:
        bound = bind_repository(
            project,
            repository_id=body.repository_id,
            full_name=body.full_name,
            base_branch=body.base_branch,
        )
    except BoundaryError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    await store.update_project(bound)
    return _access_of(bound)


@router.post("/projects/{project_id}/access/elevate", response_model=AccessDto)
async def elevate_access(project_id: str, identity: CurrentIdentity, request: Request) -> AccessDto:
    """Widen to WRITE_PR. Explicit, recorded, and reversible."""
    store = _store(request)
    try:
        project = await store.get_project(project_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from exc

    try:
        elevated = elevate_to_write(project, actor_uid=identity.subject)
    except NoRepositoryBoundError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except AccessModeError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    await store.update_project(elevated)
    return _access_of(elevated)


@router.post("/projects/{project_id}/access/revoke", response_model=AccessDto)
async def revoke_access(project_id: str, identity: CurrentIdentity, request: Request) -> AccessDto:
    store = _store(request)
    try:
        project = await store.get_project(project_id, identity.subject)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from exc

    revoked = revoke_write(project)
    await store.update_project(revoked)
    return _access_of(revoked)


def _access_of(project: Project) -> AccessDto:
    return AccessDto(
        project_id=project.id,
        access_mode=project.access_mode,
        repository_full_name=project.repository_full_name,
        base_branch=project.base_branch,
        elevated_by=project.elevated_by,
        elevated_at=project.elevated_at,
    )
