"""Repository boundary — 03_SECURITY_ACCESS.md §5, ticket F3-02.

A project is bound to exactly one repository. Every repository operation calls
`assert_within_boundary` first, so an operation against the wrong repository is
a hard error rather than a silent success on someone else's code.

This is the T3/T9 control, and it is one function on purpose: a boundary check
spread across call sites is a boundary check that will be forgotten at one.
"""

from __future__ import annotations

from mcpforge.models.core import AccessMode, Project


class BoundaryError(Exception):
    """An operation targeted a repository the project is not bound to."""


class NoRepositoryBoundError(BoundaryError):
    """The project has no repository. A demo project is always in this state."""


class AccessModeError(Exception):
    """A write was attempted on a project that is not in WRITE_PR mode."""


def assert_within_boundary(project: Project, repository_full_name: str) -> None:
    """Every repository operation starts here. No exceptions, no fast paths."""
    if project.repository_id is None or project.repository_full_name is None:
        raise NoRepositoryBoundError(
            f"Project {project.id} has no bound repository. A demo project cannot "
            "reach any repository operation."
        )
    if project.repository_full_name != repository_full_name:
        raise BoundaryError(
            f"Project {project.id} is bound to {project.repository_full_name}, "
            f"not {repository_full_name}."
        )


def assert_may_write(project: Project) -> None:
    """Guards the branch and pull-request writer.

    A demo project can never pass this, because it can never be bound to a
    repository and elevation requires one.
    """
    if project.repository_id is None:
        raise NoRepositoryBoundError(
            f"Project {project.id} has no bound repository and can never open a pull request."
        )
    if project.access_mode is not AccessMode.WRITE_PR:
        raise AccessModeError(
            f"Project {project.id} is {project.access_mode.value}. Writing requires an "
            "explicit, recorded elevation to WRITE_PR first."
        )


def bind_repository(
    project: Project, *, repository_id: str, full_name: str, base_branch: str
) -> Project:
    """Bind a project to a repository. Rebinding is refused.

    A project that could be silently repointed would make every later boundary
    check meaningless.
    """
    if project.repository_id is not None and project.repository_id != repository_id:
        raise BoundaryError(
            f"Project {project.id} is already bound to {project.repository_full_name}. "
            "Create a new project rather than repointing this one."
        )
    return project.model_copy(
        update={
            "repository_id": repository_id,
            "repository_full_name": full_name,
            "base_branch": base_branch,
            # Binding never widens access. Elevation is a separate, recorded act.
            "access_mode": project.access_mode,
        }
    )


def elevate_to_write(project: Project) -> Project:
    """Widen access. Only ever called from the explicit elevation endpoint."""
    if project.repository_id is None:
        raise NoRepositoryBoundError(
            "A project with no bound repository cannot be elevated. This is what "
            "keeps a demo project permanently unable to open a pull request."
        )
    return project.model_copy(update={"access_mode": AccessMode.WRITE_PR})


def revoke_write(project: Project) -> Project:
    """Elevation is reversible."""
    return project.model_copy(update={"access_mode": AccessMode.READ_ONLY})
