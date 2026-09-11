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
    Artifact,
    ArtifactKind,
    Origin,
    Project,
    RunEvent,
    RunState,
    Session,
    Turn,
    artifact_hash,
)
from mcpforge.store.memory import InMemoryStore
from mcpforge.store.port import NotFoundError, Store
from tests.fake_firestore import FakeFirestore
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


#: Every adapter runs this same suite, unchanged. `firestore-fake` runs the real
#: `FirestoreStore` over `tests/fake_firestore.py`, which applies Firestore's
#: value rules, on every run. Real Firestore is opt-in because it needs a
#: database: MCPFORGE_TEST_FIRESTORE=1 with ADC configured.
ADAPTERS = ["memory", "firestore-fake"]
if os.environ.get("MCPFORGE_TEST_FIRESTORE") == "1":
    ADAPTERS.append("firestore")


@pytest.fixture(params=ADAPTERS)
def store(request: pytest.FixtureRequest) -> Iterator[Store]:
    if request.param == "memory":
        yield InMemoryStore()
        return

    if request.param == "firestore-fake":
        from mcpforge.store.firestore import FirestoreStore

        yield FirestoreStore("fake-project", client=FakeFirestore())
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


# -- artifacts -------------------------------------------------------------


async def test_artifact_round_trip(store: Store) -> None:
    _project, session = await make_session(store)
    await store.put_artifact(
        Artifact(
            session_id=session.id,
            project_id=session.project_id,
            kind=ArtifactKind.TOOL_PLAN,
            payload={"tools": ["search_rooms"]},
        )
    )
    stored = await store.get_artifact(session.id, ArtifactKind.TOOL_PLAN, OWNER)
    assert stored is not None
    assert stored.payload == {"tools": ["search_rooms"]}


async def test_a_missing_artifact_is_none_not_an_error(store: Store) -> None:
    _project, session = await make_session(store)
    assert await store.get_artifact(session.id, ArtifactKind.PATCH, OWNER) is None


async def test_writing_an_artifact_again_replaces_it(store: Store) -> None:
    """Regeneration must replace, so the derived hash changes and any approval
    bound to the previous hash stops covering it."""
    _project, session = await make_session(store)
    first = Artifact(
        session_id=session.id,
        project_id=session.project_id,
        kind=ArtifactKind.TOOL_PLAN,
        payload={"tools": ["a"]},
    )
    await store.put_artifact(first)
    second = Artifact(
        session_id=session.id,
        project_id=session.project_id,
        kind=ArtifactKind.TOOL_PLAN,
        payload={"tools": ["a", "b"]},
    )
    await store.put_artifact(second)

    stored = await store.get_artifact(session.id, ArtifactKind.TOOL_PLAN, OWNER)
    assert stored is not None
    assert stored.payload == {"tools": ["a", "b"]}
    assert stored.hash != first.hash


async def test_artifact_kinds_do_not_collide(store: Store) -> None:
    _project, session = await make_session(store)
    for kind in (ArtifactKind.TOOL_PLAN, ArtifactKind.PATCH):
        await store.put_artifact(
            Artifact(
                session_id=session.id,
                project_id=session.project_id,
                kind=kind,
                payload={"kind": kind.value},
            )
        )
    plan = await store.get_artifact(session.id, ArtifactKind.TOOL_PLAN, OWNER)
    patch = await store.get_artifact(session.id, ArtifactKind.PATCH, OWNER)
    assert plan is not None and patch is not None
    assert plan.payload != patch.payload


async def test_another_user_cannot_read_an_artifact(store: Store) -> None:
    _project, session = await make_session(store)
    await store.put_artifact(
        Artifact(
            session_id=session.id,
            project_id=session.project_id,
            kind=ArtifactKind.PATCH,
            payload={"secret": "not yours"},
        )
    )
    with pytest.raises(NotFoundError):
        await store.get_artifact(session.id, ArtifactKind.PATCH, OTHER)


async def test_an_artifact_hash_is_derived_from_content_not_stored(store: Store) -> None:
    """The hash an approval binds to must come from the payload, so a caller
    cannot present content and claim an unrelated hash for it."""
    _project, session = await make_session(store)
    artifact = Artifact(
        session_id=session.id,
        project_id=session.project_id,
        kind=ArtifactKind.PATCH,
        payload={"files": 3},
    )
    assert artifact.hash == artifact_hash({"files": 3})
    assert "hash" not in artifact.model_dump()


# -- everything a run persists round-trips (T2) ----------------------------
#
# Payloads use the shapes the pipeline stores: nested maps, lists of maps,
# nulls, unicode and multi-line file contents. The fake adapter applies
# Firestore's value rules, so a shape Firestore would refuse fails here.

RUN_PAYLOADS: dict[ArtifactKind, dict[str, object]] = {
    ArtifactKind.ANALYSIS: {"workflows": [{"id": "wf_1", "name": "Book a room", "steps": 3}]},
    ArtifactKind.REPOSITORY_BINDING: {"repository": "acme/hotel", "branch": "main", "id": 4242},
    ArtifactKind.WORKFLOW_SELECTION: {"workflow_ids": ["wf_1", "wf_2"]},
    ArtifactKind.TOOL_PLAN: {
        "plan": {"tools": [{"name": "search_rooms", "input_schema": {"type": "object"}}]}
    },
    ArtifactKind.PATCH: {
        "base_commit": "abc123",
        "files": [{"path": "src/webmcp.ts", "kind": "ADD", "contents": "export {};\n// é\n"}],
    },
    ArtifactKind.SECURITY_REVIEW: {
        "completed": True,
        "verdict": {"passed": False, "reason": "blocked", "findings": [{"id": "F1"}]},
    },
    ArtifactKind.VALIDATION: {
        "completed": False,
        "validated": False,
        "reason": "Could not validate",
        "dependencies": None,
    },
}


def test_every_artifact_kind_is_covered() -> None:
    assert set(RUN_PAYLOADS) == set(ArtifactKind)


@pytest.mark.parametrize("kind", list(ArtifactKind))
async def test_every_artifact_kind_round_trips_losslessly(store: Store, kind: ArtifactKind) -> None:
    _project, session = await make_session(store)
    written = Artifact(
        session_id=session.id, project_id=session.project_id, kind=kind, payload=RUN_PAYLOADS[kind]
    )
    await store.put_artifact(written)
    stored = await store.get_artifact(session.id, kind, OWNER)
    assert stored == written
    assert stored.hash == written.hash, "the approval hash must survive storage"


async def test_a_decided_approval_round_trips_losslessly(store: Store) -> None:
    _project, session = await make_session(store)
    approval = await store.create_approval(
        Approval(
            project_id=session.project_id,
            session_id=session.id,
            gate=ApprovalGate.PULL_REQUEST,
            artifact_hash=artifact_hash({"files": 1}),
            summary="Open a pull request",
        )
    )
    decided = approval.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "actor_uid": OWNER,
            "decided_at": approval.requested_at,
        }
    )
    await store.update_approval(decided, OWNER)
    assert await store.get_approval(approval.id, OWNER) == decided


async def test_a_session_state_change_round_trips(store: Store) -> None:
    _project, session = await make_session(store)
    moved = session.model_copy(update={"state": RunState.COMPLETE})
    await store.update_session(moved)
    assert await store.get_session(session.id, OWNER) == moved


async def test_the_pull_request_result_round_trips_on_its_event(store: Store) -> None:
    """The PR result is recorded as the "Pull request opened" event."""
    _project, session = await make_session(store)
    event = RunEvent(
        session_id=session.id,
        kind="artifact.ready",
        label="Pull request opened",
        detail={"url": "https://github.test/acme/hotel/pull/12", "number": 12, "branch": "b"},
    )
    await store.append_event(event)
    assert await store.list_events(session.id, OWNER) == [event]


async def test_events_and_turns_keep_write_order_when_timestamps_tie(store: Store) -> None:
    _project, session = await make_session(store)
    tied = RunEvent(session_id=session.id, kind="k", label="x").created_at
    labels = [f"event-{i}" for i in range(12)]
    for label in labels:
        await store.append_event(
            RunEvent(session_id=session.id, kind="k", label=label, created_at=tied)
        )
        await store.append_turn(
            Turn(session_id=session.id, role="user", text=label, created_at=tied)
        )
    assert [e.label for e in await store.list_events(session.id, OWNER)] == labels
    assert [t.text for t in await store.list_turns(session.id, OWNER)] == labels
