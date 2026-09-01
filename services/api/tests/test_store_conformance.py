"""Store conformance — F2-02.

One suite, run against every adapter. When the Firestore adapter lands it is
added to the fixture params and must pass unchanged, so behaviour cannot drift
between adapters.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    ApprovalStatus,
    Origin,
    Project,
    RunEvent,
    Session,
    Turn,
    artifact_hash,
)
from mcpforge.store.memory import InMemoryStore
from mcpforge.store.port import NotFoundError, Store

OWNER = "uid-owner"
OTHER = "uid-stranger"


@pytest.fixture(params=["memory"])
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    if request.param == "memory":
        yield InMemoryStore()
    else:  # pragma: no cover - added with the Firestore adapter
        raise AssertionError(f"unknown adapter {request.param}")


async def make_session(store: Store, owner: str = OWNER) -> tuple[Project, Session]:
    project = await store.create_project(Project(owner_uid=owner, name="hotel app"))
    session = await store.create_session(Session(project_id=project.id, owner_uid=owner))
    return project, session


def test_adapter_satisfies_the_port(store: Store) -> None:
    assert isinstance(store, Store)


async def test_project_round_trip(store: Store) -> None:
    project = await store.create_project(Project(owner_uid=OWNER, name="hotel app"))
    assert (await store.get_project(project.id, OWNER)).name == "hotel app"
    assert [p.id for p in await store.list_projects(OWNER)] == [project.id]


async def test_a_project_starts_read_only(store: Store) -> None:
    project = await store.create_project(Project(owner_uid=OWNER, name="x"))
    assert project.access_mode.value == "READ_ONLY"


async def test_a_project_without_a_repository_is_a_demo(store: Store) -> None:
    demo = await store.create_project(Project(owner_uid=OWNER, name="demo"))
    real = await store.create_project(Project(owner_uid=OWNER, name="real", repository_id="12345"))
    assert demo.is_demo is True
    assert real.is_demo is False


# -- ownership -------------------------------------------------------------


async def test_another_user_cannot_read_a_project(store: Store) -> None:
    project = await store.create_project(Project(owner_uid=OWNER, name="private"))
    with pytest.raises(NotFoundError):
        await store.get_project(project.id, OTHER)


async def test_another_user_sees_no_projects(store: Store) -> None:
    await store.create_project(Project(owner_uid=OWNER, name="private"))
    assert await store.list_projects(OTHER) == []


async def test_another_user_cannot_read_a_session_or_its_contents(store: Store) -> None:
    _project, session = await make_session(store)
    await store.append_turn(Turn(session_id=session.id, role="user", text="hello"))
    with pytest.raises(NotFoundError):
        await store.get_session(session.id, OTHER)
    with pytest.raises(NotFoundError):
        await store.list_turns(session.id, OTHER)
    with pytest.raises(NotFoundError):
        await store.list_events(session.id, OTHER)


async def test_missing_and_forbidden_are_the_same_error(store: Store) -> None:
    """The store must not reveal that another user's project exists."""
    project = await store.create_project(Project(owner_uid=OWNER, name="private"))
    with pytest.raises(NotFoundError) as forbidden:
        await store.get_project(project.id, OTHER)
    with pytest.raises(NotFoundError) as missing:
        await store.get_project("proj_doesnotexist", OTHER)
    assert type(forbidden.value) is type(missing.value)


# -- conversation and activity --------------------------------------------


async def test_turns_are_returned_in_order(store: Store) -> None:
    _project, session = await make_session(store)
    for text in ("first", "second", "third"):
        await store.append_turn(Turn(session_id=session.id, role="user", text=text))
    assert [t.text for t in await store.list_turns(session.id, OWNER)] == [
        "first",
        "second",
        "third",
    ]


async def test_events_record_their_origin(store: Store) -> None:
    _project, session = await make_session(store)
    await store.append_event(
        RunEvent(
            session_id=session.id,
            kind="step.started",
            label="Scanning repository",
            origin=Origin.AGENT,
        )
    )
    events = await store.list_events(session.id, OWNER)
    assert events[0].origin is Origin.AGENT


async def test_stored_objects_are_copies_not_live_references(store: Store) -> None:
    """A caller mutating what it got back must not corrupt the store."""
    project = await store.create_project(Project(owner_uid=OWNER, name="original"))
    fetched = await store.get_project(project.id, OWNER)
    fetched.name = "tampered"
    assert (await store.get_project(project.id, OWNER)).name == "original"


# -- approvals -------------------------------------------------------------


async def test_approval_starts_pending(store: Store) -> None:
    _project, session = await make_session(store)
    approval = await store.create_approval(
        Approval(
            project_id=session.project_id,
            session_id=session.id,
            gate=ApprovalGate.TOOL_PLAN,
            artifact_hash=artifact_hash({"tools": []}),
            summary="4 tools",
        )
    )
    assert approval.status is ApprovalStatus.PENDING
    assert approval.actor_uid is None


async def test_find_approval_matches_only_an_approved_record_for_that_artifact(
    store: Store,
) -> None:
    _project, session = await make_session(store)
    plan_hash = artifact_hash({"tools": ["search_hotels"]})
    approval = Approval(
        project_id=session.project_id,
        session_id=session.id,
        gate=ApprovalGate.TOOL_PLAN,
        artifact_hash=plan_hash,
        summary="1 tool",
    )
    await store.create_approval(approval)

    # Pending does not count.
    assert await store.find_approval(session.id, ApprovalGate.TOOL_PLAN, plan_hash, OWNER) is None

    approval.status = ApprovalStatus.APPROVED
    approval.actor_uid = OWNER
    await store.update_approval(approval, OWNER)

    assert (
        await store.find_approval(session.id, ApprovalGate.TOOL_PLAN, plan_hash, OWNER)
    ) is not None
    # A different gate is not covered.
    assert await store.find_approval(session.id, ApprovalGate.PATCH, plan_hash, OWNER) is None
    # A different artifact is not covered.
    other = artifact_hash({"tools": ["search_hotels", "cancel_booking"]})
    assert await store.find_approval(session.id, ApprovalGate.TOOL_PLAN, other, OWNER) is None


async def test_another_user_cannot_write_an_approval(store: Store) -> None:
    """Ownership is checked on write, not only on the read that preceded it."""
    _project, session = await make_session(store)
    approval = await store.create_approval(
        Approval(
            project_id=session.project_id,
            session_id=session.id,
            gate=ApprovalGate.TOOL_PLAN,
            artifact_hash="abc",
            summary="plan",
        )
    )
    approval.status = ApprovalStatus.APPROVED
    with pytest.raises(NotFoundError):
        await store.update_approval(approval, OTHER)


async def test_another_user_cannot_look_up_an_approval(store: Store) -> None:
    _project, session = await make_session(store)
    approval = await store.create_approval(
        Approval(
            project_id=session.project_id,
            session_id=session.id,
            gate=ApprovalGate.PATCH,
            artifact_hash="abc",
            summary="patch",
        )
    )
    with pytest.raises(NotFoundError):
        await store.get_approval(approval.id, OTHER)
