"""Write-pipeline failure handling — F6-04."""

from __future__ import annotations

import pytest

from mcpforge.orchestration.recovery import (
    WriteStage,
    may_delete_branch,
    outcome_for,
)

BRANCH = "mcpforge/webmcp-booking"


def test_success_says_what_to_do_next() -> None:
    outcome = outcome_for(WriteStage.PULL_REQUEST_OPENED, branch=BRANCH)
    assert outcome.succeeded is True
    assert "Review and merge" in outcome.explain()


@pytest.mark.parametrize(
    "stage",
    [WriteStage.NOTHING_DONE, WriteStage.COMMIT_CREATED, WriteStage.BRANCH_CREATED],
)
def test_every_failure_point_leaves_an_explained_state(stage: WriteStage) -> None:
    """04_FRONTEND_SPEC.md §10 — never a generic "something went wrong"."""
    outcome = outcome_for(stage, branch=BRANCH, error="403 from GitHub")
    assert outcome.succeeded is False
    explanation = outcome.explain()
    assert "403 from GitHub" in explanation
    assert len(explanation) > 30


def test_a_failure_before_any_write_says_nothing_changed() -> None:
    outcome = outcome_for(WriteStage.NOTHING_DONE, branch=None, error="approval missing")
    assert "Nothing was written" in outcome.explain()


def test_an_orphaned_commit_is_explained_as_harmless() -> None:
    """A commit with no ref pointing at it changes nothing the developer sees."""
    outcome = outcome_for(WriteStage.COMMIT_CREATED, branch=BRANCH, error="ref creation failed")
    explanation = outcome.explain()
    assert "no branch points at it" in explanation
    assert "nothing in your repository changed" in explanation


def test_a_leftover_branch_is_named_so_it_can_be_removed() -> None:
    outcome = outcome_for(WriteStage.BRANCH_CREATED, branch=BRANCH, error="PR failed")
    assert BRANCH in outcome.explain()
    assert "delete it or retry" in outcome.explain()


def test_cleanup_is_reported_when_it_happened() -> None:
    outcome = outcome_for(
        WriteStage.BRANCH_CREATED, branch=BRANCH, error="PR failed", cleanup_performed=True
    )
    assert "was removed" in outcome.explain()


# -- cleanup must never delete something we did not create ------------------


def test_cleanup_removes_a_branch_this_run_created() -> None:
    assert may_delete_branch(BRANCH, created_by_this_run=True) is True


def test_cleanup_never_touches_a_branch_this_run_did_not_create() -> None:
    """A developer may have their own mcpforge/* branch. Matching a pattern is
    not permission to delete it."""
    assert may_delete_branch(BRANCH, created_by_this_run=False) is False


@pytest.mark.parametrize("branch", ["main", "develop", "feature/login", "mcpforge", "release"])
def test_cleanup_never_touches_a_branch_outside_our_namespace(branch: str) -> None:
    assert may_delete_branch(branch, created_by_this_run=True) is False


def test_both_conditions_are_required() -> None:
    """Neither alone is sufficient, so removing either check fails a test."""
    assert may_delete_branch("main", created_by_this_run=True) is False
    assert may_delete_branch(BRANCH, created_by_this_run=False) is False
    assert may_delete_branch(BRANCH, created_by_this_run=True) is True
