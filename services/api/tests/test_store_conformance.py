"""Store conformance — F2-02.

One suite, run against every adapter. When the Firestore adapter lands it is
added to the fixture params and must pass unchanged, so behaviour cannot drift
between adapters.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from uuid import uuid4

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
from tests.structure import SRC, code_lines, files_importing

# Owner ids are unique per test. The in-memory adapter starts empty every time,
# but Firestore is persistent: with a fixed owner id, documents from earlier
# tests and earlier runs would accumulate and list_projects would keep growing.
# The shared suite surfaced that difference, which is the point of running one
# suite against both adapters.
OWNER = "uid-owner"
OTHER = "uid-stranger"


@pytest.fixture(autouse=True)
def unique_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    run = uuid4().hex[:12]
    monkeypatch.setattr(sys.modules[__name__], "OWNER", f"uid-owner-{run}")
    monkeypatch.setattr(sys.modules[__name__], "OTHER", f"uid-stranger-{run}")


#: Every adapter runs this same suite, unchanged. Firestore is opt-in because it
#: needs a real database: MCPFORGE_TEST_FIRESTORE=1 with ADC configured.
ADAPTERS = ["memory"]
if os.environ.get("MCPFORGE_TEST_FIRESTORE") == "1":
    ADAPTERS.append("firestore")


@pytest.fixture(params=ADAPTERS)
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    if request.param == "memory":
        yield InMemoryStore()
        return

    if request.param == "firestore":
        from mcpforge.store.firestore import FirestoreStore

        # Read from a dedicated variable: the isolation fixture in conftest
        # deliberately clears FIREBASE_PROJECT_ID so no test inherits local
        # configuration, and that applies here too.
        project_id = os.environ.get("MCPFORGE_TEST_FIRESTORE_PROJECT")
        assert project_id, (
            "Set MCPFORGE_TEST_FIRESTORE_PROJECT to the Firebase project id "
            "when running the Firestore adapter tests"
        )
        # Documents are namespaced per run by the ids the models generate, so
        # concurrent runs cannot collide.
        yield FirestoreStore(project_id)
        return

    raise AssertionError(f"unknown adapter {request.param}")


async def make_session(store: Store, owner: str | None = None) -> tuple[Project, Session]:
    # Read OWNER at call time, not as a default: defaults bind at import, which
    # would defeat the per-test identity fixture above.
    owner = owner or OWNER
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


def test_no_firestore_sdk_is_imported_outside_its_adapter() -> None:
    """The store can move from in-memory to Firestore without touching anything
    above it. That only holds if the SDK stays inside its own adapter."""
    offenders = files_importing(
        ("google.cloud", "google.cloud.firestore"), exclude=("firestore.py",)
    )
    assert not offenders, "Firestore SDK imported outside its adapter:\n" + "\n".join(offenders)


def test_the_firestore_adapter_uses_no_service_account_key() -> None:
    """03_SECURITY_ACCESS.md §9 — ADC only, no key file, anywhere."""
    adapter = SRC / "mcpforge" / "store" / "firestore.py"
    banned = ("GOOGLE_APPLICATION_CREDENTIALS", "from_service_account", "service_account_json")
    offenders = [
        f"{adapter.name}:{lineno}: {code}"
        for lineno, code in code_lines(adapter)
        for term in banned
        if term in code
    ]
    assert not offenders, "key material referenced:\n" + "\n".join(offenders)


def test_ownership_is_filtered_in_the_query_not_after_the_fetch() -> None:
    """A mis-scoped read must return nothing, rather than returning another
    user's document and relying on us to drop it afterwards."""
    adapter = SRC / "mcpforge" / "store" / "firestore.py"
    code = "\n".join(line for _, line in code_lines(adapter))
    assert 'FieldFilter("owner_uid", "==", owner_uid)' in code, (
        "list_projects must filter by owner in the Firestore query"
    )
