"""Failure handling for the write pipeline — ticket F6-04.

Creating a branch, committing and opening a pull request is three API calls, and
any of them can fail. Each failure point must leave a state the developer can
understand, and cleanup must never remove something MCPForge did not create.

The asymmetry matters: a leftover branch is untidy, deleting the wrong branch is
data loss. So cleanup is narrow — only a branch under `mcpforge/`, only one this
run created, and only when we know the pull request was never opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mcpforge.github.branches import BRANCH_PREFIX
from mcpforge.logging import get_logger

log = get_logger(__name__)


class WriteStage(StrEnum):
    """Where the write pipeline got to. Recorded, so a failure is explainable."""

    NOTHING_DONE = "NOTHING_DONE"
    COMMIT_CREATED = "COMMIT_CREATED"
    BRANCH_CREATED = "BRANCH_CREATED"
    PULL_REQUEST_OPENED = "PULL_REQUEST_OPENED"


@dataclass(frozen=True)
class WriteOutcome:
    """What happened, and what the developer should do about it."""

    stage: WriteStage
    branch: str | None
    error: str | None = None
    cleanup_performed: bool = False
    #: Set only on success. Typed as object so this module does not depend on
    #: the writer, which depends on it.
    pull_request: object | None = None

    @property
    def succeeded(self) -> bool:
        return self.stage is WriteStage.PULL_REQUEST_OPENED and self.error is None

    def explain(self) -> str:
        """Plain language for the UI. Never a generic failure message."""
        if self.succeeded:
            return "The pull request is open. Review and merge it as you normally would."

        if self.stage is WriteStage.NOTHING_DONE:
            return f"Nothing was written to your repository. {self.error or ''}".strip()
        if self.stage is WriteStage.COMMIT_CREATED:
            return (
                "A commit object was created but no branch points at it, so nothing in "
                f"your repository changed. Git will collect it. {self.error or ''}".strip()
            )
        if self.stage is WriteStage.BRANCH_CREATED:
            cleaned = (
                f"The branch {self.branch} was removed."
                if self.cleanup_performed
                else f"The branch {self.branch} is still there; delete it or retry."
            )
            return (
                f"The branch was created but the pull request was not opened. {cleaned} "
                f"{self.error or ''}"
            ).strip()
        return self.error or "The write failed."


def may_delete_branch(branch: str, *, created_by_this_run: bool) -> bool:
    """Whether cleanup may remove this branch.

    Two conditions, both required. The prefix check alone is not enough: a
    developer may have made their own `mcpforge/...` branch, and deleting it
    because it matches a pattern would be exactly the destructive cleanup this
    function exists to prevent.
    """
    return created_by_this_run and branch.startswith(BRANCH_PREFIX)


def outcome_for(
    stage: WriteStage,
    *,
    branch: str | None,
    error: Exception | str | None = None,
    cleanup_performed: bool = False,
    pull_request: object | None = None,
) -> WriteOutcome:
    message = str(error) if error is not None else None
    if message:
        log.warning(
            "github.write_failed",
            stage=stage.value,
            branch=branch,
            cleanup_performed=cleanup_performed,
        )
    return WriteOutcome(
        stage=stage,
        branch=branch,
        error=message,
        cleanup_performed=cleanup_performed,
        pull_request=pull_request,
    )
