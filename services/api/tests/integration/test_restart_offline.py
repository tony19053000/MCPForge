"""T2 — a run survives a process restart.

The first app drives the whole PR leg through the real routes and gates, which
persists every artifact kind, every approval and the pull-request result. Then
it is discarded. A second app gets a new `FirestoreStore` over the same
database, as a restarted process would, and must read all of it back.

The database is `tests/fake_firestore.py`. It is not Firestore: it applies
Firestore's value rules, so the pipeline's real payloads are checked for shapes
Firestore would refuse, but the network and IAM are covered only by the opt-in
live conformance run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcpforge.auth.firebase import FirebaseIdTokenVerifier
from mcpforge.config import Settings
from mcpforge.github.client import GitHubAppClient
from mcpforge.main import create_app
from mcpforge.models.core import ApprovalStatus, ArtifactKind
from mcpforge.orchestration.pipeline import PipelineOptions
from mcpforge.store.firestore import FirestoreStore
from mcpforge.store.port import NotFoundError, Store
from tests.conftest import TEST_PROJECT, MakeToken, StubJWKClient
from tests.fake_firestore import FakeFirestore
from tests.integration.harness import DEMO_APP, Driver
from tests.integration.test_pipeline_offline import (
    OWNER,
    FakeGitHub,
    Rig,
    ScriptedCommandExecutor,
    ScriptedGemini,
    run_pr_leg,
)

STRANGER = "someone-else"


@pytest.fixture
def app_factory(
    tmp_path: Path, settings: Settings, jwks: StubJWKClient, private_pem: str
) -> Callable[[Store], tuple[FastAPI, ScriptedGemini, ScriptedCommandExecutor, FakeGitHub]]:
    key = tmp_path / "app.pem"
    key.write_text(private_pem)
    scripted_tree = tmp_path / "scripted-node-modules"
    (scripted_tree / "vitest").mkdir(parents=True)
    (scripted_tree / "vitest" / "vitest.mjs").write_text("// scripted; never executed\n")

    def build(
        store: Store,
    ) -> tuple[FastAPI, ScriptedGemini, ScriptedCommandExecutor, FakeGitHub]:
        github = FakeGitHub()
        gemini = ScriptedGemini()
        executor = ScriptedCommandExecutor(tmp_path / "workspaces")
        app = create_app(
            settings,
            token_verifier=FirebaseIdTokenVerifier(TEST_PROJECT, jwks_client=jwks),  # type: ignore[arg-type]
            store=store,
            gemini=gemini,
            github=GitHubAppClient(
                app_id="12345",
                private_key_path=str(key),
                http=httpx.AsyncClient(transport=github.transport()),
            ),
            executor=executor,
            pipeline_options=PipelineOptions(
                demo_fixture=DEMO_APP,
                demo_dependency_tree=scripted_tree,
                writer_transport=github.transport(),
            ),
        )
        return app, gemini, executor, github

    return build


def _artifacts(store: Store, session_id: str) -> dict[ArtifactKind, Any]:
    return {kind: asyncio.run(store.get_artifact(session_id, kind, OWNER)) for kind in ArtifactKind}


def test_a_completed_run_is_readable_after_a_restart(
    app_factory: Callable[[Store], tuple[FastAPI, Any, Any, Any]], make_token: MakeToken
) -> None:
    database = FakeFirestore()
    token = make_token(subject=OWNER)

    # -- before the restart: a full PR-leg run ------------------------------
    first, gemini, executor, github = app_factory(FirestoreStore("unused", client=database))
    with TestClient(first) as client:
        rig = Rig(Driver(client, token), gemini, executor, github)
        session_id = run_pr_leg(rig)
        events_before = rig.driver.events(session_id)
        artifacts_before = _artifacts(first.state.store, session_id)
    del first, rig

    # -- after: a new process, a new store object, the same database ----------
    restarted_store = FirestoreStore("unused", client=database)
    second, *_ = app_factory(restarted_store)
    with TestClient(second) as client:
        d = Driver(client, token)

        assert d.stored_state(session_id, OWNER) == "COMPLETE"
        assert d.events(session_id) == events_before, "the timeline changed across restart"

        # Every artifact kind the run stored is back, byte for byte.
        artifacts_after = _artifacts(restarted_store, session_id)
        assert all(artifacts_after.values()), [k for k, v in artifacts_after.items() if not v]
        assert artifacts_after == artifacts_before

        # Every approval the developer granted is back, still granted by them.
        decided = [e for e in events_before if e["kind"] == "approval.decided"]
        assert len(decided) == 5
        for event in decided:
            response = d.get(f"/api/approvals/{event['detail']['approval_id']}")
            assert response.status_code == 200, response.text
            approval = response.json()
            assert approval["status"] == ApprovalStatus.APPROVED.value
            assert approval["actor_uid"] == OWNER

        # The pull-request result is back.
        opened = [e for e in d.events(session_id) if e["label"] == "Pull request opened"]
        assert opened and opened[0]["detail"]["url"].endswith("/pull/12")

        # The read tools answer from the persisted stage output.
        for read in ("workflows", "plan", "validation"):
            assert d.agent_read(session_id, read)["available"] is True, read

        # Owner scoping survives the restart too.
        stranger = Driver(client, make_token(subject=STRANGER))
        for event in decided:
            refused = stranger.get(f"/api/approvals/{event['detail']['approval_id']}")
            assert refused.status_code == 404
        with pytest.raises(NotFoundError):
            asyncio.run(restarted_store.get_session(session_id, STRANGER))
