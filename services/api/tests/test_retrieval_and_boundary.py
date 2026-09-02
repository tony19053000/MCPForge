"""Context retrieval (F3-06) and repository boundary (F3-02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcpforge.github.boundary import (
    AccessModeError,
    BoundaryError,
    NoRepositoryBoundError,
    assert_may_write,
    assert_within_boundary,
    bind_repository,
    elevate_to_write,
    revoke_write,
)
from mcpforge.indexing.indexer import build_index
from mcpforge.indexing.retrieval import (
    BudgetExceededError,
    ContextRetriever,
    QuarantinedFileError,
    RetrievalRequest,
    estimate_tokens,
)
from mcpforge.models.core import AccessMode, Project
from mcpforge.models.index import FileKind, RepositoryIndex

DEMO = Path(__file__).resolve().parents[3] / "fixtures" / "demo-hotel-app"


@pytest.fixture(scope="module")
def index() -> RepositoryIndex:
    return build_index(DEMO)


@pytest.fixture
def retriever(index: RepositoryIndex) -> ContextRetriever:
    return ContextRetriever(index, DEMO)


# -- retrieval --------------------------------------------------------------


def test_required_files_are_returned(retriever: ContextRetriever) -> None:
    context = retriever.retrieve(RetrievalRequest(required=["src/lib/rooms.ts"]))
    assert [s.path for s in context.snippets] == ["src/lib/rooms.ts"]
    assert "searchRooms" in context.snippets[0].text


def test_a_symbol_pulls_in_the_file_that_defines_it(retriever: ContextRetriever) -> None:
    context = retriever.retrieve(RetrievalRequest(symbols=["createReservation"]))
    assert [s.path for s in context.snippets] == ["src/lib/reservations.ts"]
    assert "defines createReservation" in context.snippets[0].reason


def test_preferred_kinds_fill_the_remaining_budget(retriever: ContextRetriever) -> None:
    context = retriever.retrieve(RetrievalRequest(preferred_kinds=[FileKind.SERVICE]))
    paths = {s.path for s in context.snippets}
    assert paths == {"src/lib/rooms.ts", "src/lib/availability.ts", "src/lib/reservations.ts"}


def test_rendered_context_labels_every_snippet_with_its_path(
    retriever: ContextRetriever,
) -> None:
    """An agent claim has to be traceable back to a file, or evidence means nothing."""
    rendered = retriever.retrieve(RetrievalRequest(required=["src/lib/rooms.ts"])).render()
    assert "--- src/lib/rooms.ts (required) ---" in rendered


def test_the_budget_is_respected(retriever: ContextRetriever) -> None:
    context = retriever.retrieve(
        RetrievalRequest(preferred_kinds=[FileKind.SERVICE, FileKind.COMPONENT], token_budget=200)
    )
    assert context.total_tokens <= 200
    assert context.omitted, "something should have been left out at this budget"


def test_required_evidence_that_does_not_fit_fails_loudly(
    retriever: ContextRetriever,
) -> None:
    """A silently truncated prompt produces confident nonsense. Fail instead."""
    with pytest.raises(BudgetExceededError) as exc:
        retriever.retrieve(RetrievalRequest(required=["src/lib/reservations.ts"], token_budget=10))
    assert "src/lib/reservations.ts" in str(exc.value)
    assert exc.value.budget == 10


def test_optional_context_is_omitted_quietly_but_reported(
    retriever: ContextRetriever,
) -> None:
    context = retriever.retrieve(
        RetrievalRequest(
            required=["src/lib/rooms.ts"],
            preferred_kinds=[FileKind.SERVICE, FileKind.COMPONENT, FileKind.ROUTE],
            token_budget=estimate_tokens(Path(DEMO / "src/lib/rooms.ts").read_text()) + 20,
        )
    )
    assert "src/lib/rooms.ts" in [s.path for s in context.snippets]
    assert context.omitted != []


def test_a_quarantined_file_can_never_be_retrieved(tmp_path: Path) -> None:
    """Defence in depth behind F3-03: even asked for by name, it is refused."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const a = 1;")
    (tmp_path / "package.json").write_text('{"dependencies":{"next":"16"}}')
    (tmp_path / ".env").write_text("SECRET=" + "AQ." + "y" * 40)

    index = build_index(tmp_path)
    retriever = ContextRetriever(index, tmp_path)

    with pytest.raises(QuarantinedFileError):
        retriever.retrieve(RetrievalRequest(required=[".env"]))


def test_a_file_outside_the_index_is_refused(retriever: ContextRetriever) -> None:
    with pytest.raises(FileNotFoundError):
        retriever.retrieve(RetrievalRequest(required=["src/lib/does-not-exist.ts"]))


def test_a_path_escaping_the_repository_is_refused(index: RepositoryIndex) -> None:
    retriever = ContextRetriever(index, DEMO)
    with pytest.raises((QuarantinedFileError, FileNotFoundError)):
        retriever.retrieve(RetrievalRequest(required=["../../.env"]))


# -- boundary ---------------------------------------------------------------


def bound_project() -> Project:
    return bind_repository(
        Project(owner_uid="u1", name="hotel"),
        repository_id="12345",
        full_name="tony19053000/mcpforge-test",
        base_branch="main",
    )


def test_binding_records_the_repository_and_keeps_read_only() -> None:
    project = bound_project()
    assert project.repository_full_name == "tony19053000/mcpforge-test"
    assert project.base_branch == "main"
    # Binding never widens access.
    assert project.access_mode is AccessMode.READ_ONLY


def test_an_operation_on_the_bound_repository_is_allowed() -> None:
    assert_within_boundary(bound_project(), "tony19053000/mcpforge-test")


def test_an_operation_on_a_different_repository_is_a_hard_error() -> None:
    """T3/T9: the check that stops MCPForge touching a repository it was not given."""
    with pytest.raises(BoundaryError, match="is bound to"):
        assert_within_boundary(bound_project(), "someone-else/private-app")


def test_a_project_cannot_be_silently_repointed() -> None:
    with pytest.raises(BoundaryError, match="already bound"):
        bind_repository(
            bound_project(), repository_id="99999", full_name="other/repo", base_branch="main"
        )


def test_rebinding_the_same_repository_is_allowed() -> None:
    """Idempotent, so a retry does not fail."""
    again = bind_repository(
        bound_project(),
        repository_id="12345",
        full_name="tony19053000/mcpforge-test",
        base_branch="main",
    )
    assert again.repository_id == "12345"


# -- demo projects can never write -----------------------------------------


def test_a_demo_project_has_no_repository() -> None:
    demo = Project(owner_uid="u1", name="demo")
    assert demo.is_demo is True
    with pytest.raises(NoRepositoryBoundError):
        assert_within_boundary(demo, "anything/at-all")


def test_a_demo_project_can_never_reach_the_writer() -> None:
    """03_SECURITY_ACCESS.md §5 — structurally impossible, and asserted."""
    with pytest.raises(NoRepositoryBoundError):
        assert_may_write(Project(owner_uid="u1", name="demo"))


def test_a_demo_project_can_never_be_elevated() -> None:
    with pytest.raises(NoRepositoryBoundError, match="permanently unable"):
        elevate_to_write(Project(owner_uid="u1", name="demo"), actor_uid="u1")


# -- access mode ------------------------------------------------------------


def test_a_read_only_project_cannot_write() -> None:
    with pytest.raises(AccessModeError, match="explicit, recorded elevation"):
        assert_may_write(bound_project())


def test_an_elevated_project_may_write() -> None:
    assert_may_write(elevate_to_write(bound_project(), actor_uid="u1"))


def test_elevation_is_reversible() -> None:
    elevated = elevate_to_write(bound_project(), actor_uid="u1")
    assert revoke_write(elevated).access_mode is AccessMode.READ_ONLY
    with pytest.raises(AccessModeError):
        assert_may_write(revoke_write(elevated))


def test_elevation_records_who_did_it_and_when() -> None:
    """03_SECURITY_ACCESS.md §5 — an elevation must be auditable, not just effective."""
    elevated = elevate_to_write(bound_project(), actor_uid="u1")
    assert elevated.elevated_by == "u1"
    assert elevated.elevated_at is not None


def test_elevation_requires_an_authenticated_actor() -> None:
    with pytest.raises(AccessModeError, match="authenticated user"):
        elevate_to_write(bound_project(), actor_uid="")


def test_only_the_owner_may_widen_access() -> None:
    with pytest.raises(AccessModeError, match="project owner"):
        elevate_to_write(bound_project(), actor_uid="uid-stranger")


def test_revoking_keeps_the_audit_trail() -> None:
    """An audit trail that vanishes when access is revoked is not an audit trail."""
    revoked = revoke_write(elevate_to_write(bound_project(), actor_uid="u1"))
    assert revoked.access_mode is AccessMode.READ_ONLY
    assert revoked.elevated_by == "u1"
    assert revoked.elevated_at is not None


def test_a_new_project_has_no_elevation_record() -> None:
    project = Project(owner_uid="u1", name="fresh")
    assert project.elevated_by is None
    assert project.elevated_at is None
