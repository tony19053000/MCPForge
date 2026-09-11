"""T2 — the running service builds its store from settings.

The Firestore cases patch the SDK's `AsyncClient` inside the adapter module, so
the real `FirestoreStore` constructor runs and only the network client is
replaced (by `tests/fake_firestore.py`). No test here touches the network.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import firestore as firestore_sdk

from mcpforge.config import ConfigError, Settings, StoreKind
from mcpforge.main import build_default_store, create_app
from mcpforge.store import firestore as firestore_module
from mcpforge.store.firestore import PROBE_DOCUMENT, PROJECTS, FirestoreStore
from mcpforge.store.memory import InMemoryStore
from mcpforge.store.port import StoreUnavailableError
from tests.conftest import TEST_PROJECT
from tests.fake_firestore import FakeFirestore


def firestore_settings(project: str | None = TEST_PROJECT) -> Settings:
    return Settings(store=StoreKind.FIRESTORE, firebase_project_id=project)


def fake_sdk(monkeypatch: pytest.MonkeyPatch, db: FakeFirestore) -> list[str | None]:
    """Replace the SDK client constructor; record the project it was given."""
    projects: list[str | None] = []

    def construct(*, project: str | None = None, **_: Any) -> FakeFirestore:
        projects.append(project)
        return db

    # The adapter resolves `firestore.AsyncClient` at call time, so patching the
    # SDK module attribute is seen by the real constructor.
    monkeypatch.setattr(firestore_sdk, "AsyncClient", construct)
    return projects


def make_app(settings: Settings, **kwargs: Any) -> Any:
    return create_app(settings, executor=None, **kwargs)


# -- selection -------------------------------------------------------------


def test_memory_is_the_default() -> None:
    assert Settings().store is StoreKind.MEMORY
    app = make_app(Settings(firebase_project_id=TEST_PROJECT))
    assert isinstance(app.state.store, InMemoryStore)
    assert app.state.store_startup_check is None


def test_the_setting_reads_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE", "firestore")
    assert Settings().store is StoreKind.FIRESTORE


def test_an_unknown_store_is_a_validation_error() -> None:
    with pytest.raises(ValueError):
        Settings(store="postgres")  # type: ignore[arg-type]


def test_firestore_constructs_a_firestore_store_on_the_firebase_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects = fake_sdk(monkeypatch, FakeFirestore())
    app = make_app(firestore_settings())

    assert isinstance(app.state.store, FirestoreStore)
    assert projects == [TEST_PROJECT]
    assert app.state.store_startup_check is not None


@pytest.mark.parametrize("project", [None, ""])
def test_firestore_without_a_project_id_refuses(
    monkeypatch: pytest.MonkeyPatch, project: str | None
) -> None:
    projects = fake_sdk(monkeypatch, FakeFirestore())
    with pytest.raises(ConfigError, match="FIREBASE_PROJECT_ID"):
        make_app(firestore_settings(project))
    assert projects == [], "no client may be built without a project"


def test_firestore_without_adc_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_credentials(**_: Any) -> None:
        raise DefaultCredentialsError("no ADC")  # type: ignore[no-untyped-call]

    monkeypatch.setattr(firestore_sdk, "AsyncClient", no_credentials)
    with pytest.raises(StoreUnavailableError, match="Application Default Credentials"):
        build_default_store(firestore_settings())


def test_an_explicit_store_wins_and_is_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    projects = fake_sdk(monkeypatch, FakeFirestore())
    explicit = InMemoryStore()
    # Would refuse if built from settings: firestore with no project id.
    app = make_app(firestore_settings(None), store=explicit)

    assert app.state.store is explicit
    assert app.state.store_startup_check is None
    assert projects == []


# -- the startup check -----------------------------------------------------


def test_a_reachable_firestore_starts_with_one_read_and_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = FakeFirestore()
    fake_sdk(monkeypatch, db)
    app = make_app(firestore_settings())
    assert db.reads == [], "nothing may be read before startup"

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200

    assert db.reads == [(PROJECTS, PROBE_DOCUMENT)]
    assert db.writes == 0


class _Unreachable(FakeFirestore):
    async def before_read(self, collection: str, doc_id: str) -> None:
        raise ConnectionError("503 Service Unavailable")


class _Stalled(FakeFirestore):
    async def before_read(self, collection: str, doc_id: str) -> None:
        await asyncio.sleep(5)


def test_an_unreachable_firestore_aborts_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sdk(monkeypatch, _Unreachable())
    app = make_app(firestore_settings())
    with pytest.raises(StoreUnavailableError, match="unreachable"), TestClient(app):
        pytest.fail("the service accepted traffic without a reachable store")


def test_a_stalled_firestore_aborts_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(firestore_module, "PROBE_TIMEOUT_SECONDS", 0.05)
    fake_sdk(monkeypatch, _Stalled())
    app = make_app(firestore_settings())
    with pytest.raises(StoreUnavailableError, match="TimeoutError"), TestClient(app):
        pytest.fail("the service accepted traffic without a reachable store")
