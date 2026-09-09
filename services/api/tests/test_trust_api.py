"""The trust panel's server state — F8-03.

The panel's whole value is that it is not flattering, so these tests are written
against the two ways it could flatter:

- reporting a quarantine result for a scan that never ran, or a count that came
  from anywhere but the filtering pipeline's own record;
- reporting an execution boundary better than the one in force.

Every fabricated attestation below is fabricated *by the test*. Nothing in the
product can produce one: `F8-02` is blocked on B-04, and
`test_no_backend_module_obtains_an_attestation_token_yet` in `test_attestation.py`
is what keeps that true.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcpforge.api import trust
from mcpforge.auth.identity import AuthError, VerifiedIdentity
from mcpforge.config import SecureExecutorKind, Settings
from mcpforge.execution.attestation import AttestationEvidence, TrustLevel
from mcpforge.execution.development import DevelopmentSecureExecutor
from mcpforge.execution.provider import (
    Command,
    CommandResult,
    SecureExecutionProvider,
    Workspace,
    WorkspaceSpec,
)
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.github.branches import BRANCH_PREFIX, branch_name_for
from mcpforge.main import create_app
from mcpforge.models.core import Artifact, ArtifactKind
from mcpforge.models.index import RepositoryIndex
from mcpforge.security import filters
from mcpforge.security.pipeline import filter_tree
from mcpforge.store.memory import InMemoryStore

OWNER = "uid-owner"
OTHER = "uid-stranger"

PLANTED_SECRET = "AQ.Ab8RN6JnotarealkeybutshapedlikeoneXXXXXXXXXXXXXXXXX"


class TokenIsUid:
    async def verify(self, raw_token: str) -> VerifiedIdentity:
        if not raw_token.startswith("uid-"):
            raise AuthError("bad token")
        return VerifiedIdentity(subject=raw_token, issuer="test")


def build_client(settings: Settings, executor: SecureExecutionProvider | None = None) -> TestClient:
    app = create_app(
        settings,
        token_verifier=TokenIsUid(),
        store=InMemoryStore(),
        gemini=FakeGeminiProvider([]),
        executor=executor,
    )
    return TestClient(app)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with build_client(settings) as c:
        yield c


def auth(uid: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def make_session(client: TestClient, uid: str = OWNER) -> tuple[str, str]:
    project = client.post("/api/projects", json={"name": "hotel"}, headers=auth(uid)).json()
    session = client.post(f"/api/projects/{project['id']}/sessions", headers=auth(uid)).json()
    return str(project["id"]), str(session["id"])


def store_analysis(client: TestClient, session_id: str, payload: dict[str, object]) -> None:
    store = client.app.state.store  # type: ignore[attr-defined]
    session = asyncio.run(store.get_session(session_id, OWNER))
    asyncio.run(
        store.put_artifact(
            Artifact(
                session_id=session_id,
                project_id=session.project_id,
                kind=ArtifactKind.ANALYSIS,
                payload=payload,
            )
        )
    )


def trust_state(client: TestClient, session_id: str, uid: str = OWNER) -> dict[str, Any]:
    response = client.get(f"/api/sessions/{session_id}/trust", headers=auth(uid))
    assert response.status_code == 200, response.text
    return dict(response.json())


# -- ownership --------------------------------------------------------------


def test_trust_state_requires_a_token(client: TestClient) -> None:
    _, session_id = make_session(client)
    assert client.get(f"/api/sessions/{session_id}/trust").status_code == 401


def test_another_users_session_is_not_readable(client: TestClient) -> None:
    _, session_id = make_session(client)
    assert client.get(f"/api/sessions/{session_id}/trust", headers=auth(OTHER)).status_code == 404


# -- boundary and access mode ----------------------------------------------


def test_boundary_and_access_mode_come_from_the_stored_project(client: TestClient) -> None:
    project_id, session_id = make_session(client)

    unbound = trust_state(client, session_id)
    assert unbound["repository"]["bound"] is False
    assert unbound["repository"]["repository_full_name"] is None
    assert unbound["access_mode"] == "READ_ONLY"

    client.post(
        f"/api/projects/{project_id}/repository",
        json={"repository_id": "9", "full_name": "acme/hotel-app", "base_branch": "main"},
        headers=auth(),
    )
    client.post(f"/api/projects/{project_id}/access/elevate", headers=auth())

    bound = trust_state(client, session_id)
    assert bound["repository"] == {
        "bound": True,
        "repository_full_name": "acme/hotel-app",
        "base_branch": "main",
        "is_demo": False,
    }
    # Elevation is shown as soon as it happens. The panel does not cache a
    # friendlier earlier answer.
    assert bound["access_mode"] == "WRITE_PR"


# -- secret filtering and the quarantine record -----------------------------


def test_nothing_analyzed_reports_absent_rather_than_zero(client: TestClient) -> None:
    """Zero would read as "scanned and clean" for a scan that never ran."""
    _, session_id = make_session(client)
    filtering = trust_state(client, session_id)["secret_filtering"]

    assert filtering["analyzed"] is False
    assert filtering["quarantined_count"] is None
    assert filtering["quarantined_paths"] == []
    assert filtering["active"] is True


def _repository_with_planted_secrets(root: Path, *, extra_secrets: int = 0) -> None:
    """Plant the standard three, plus `extra_secrets` more.

    The count varies so a test can assert the reported number *moves with* the
    fixture. A single fixed fixture cannot distinguish a real count from a
    constant that happens to equal it — the reviewer proved that by replacing
    the endpoint's `len(paths)` with a literal `3` and watching the whole suite
    stay green.
    """
    (root / "src").mkdir(parents=True)
    (root / "src" / "page.tsx").write_text("export default function Page() { return null; }\n")
    (root / ".env").write_text(f"GEMINI_API_KEY={PLANTED_SECRET}\n")
    (root / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nabc\n")
    (root / "config.ts").write_text(f'export const key = "{PLANTED_SECRET}";\n')
    for index in range(extra_secrets):
        (root / f"extra-{index}.pem").write_text("-----BEGIN PRIVATE KEY-----\nabc\n")


@pytest.mark.parametrize("extra_secrets", [0, 2])
def test_the_quarantine_count_is_the_filter_pipelines_own_record(
    client: TestClient, tmp_path: Path, extra_secrets: int
) -> None:
    """The count is a real `filter_tree` result, and it moves with the fixture.

    Parametrised deliberately. With one fixed fixture this test could not fail
    against a hardcoded number: the reviewer replaced the endpoint's
    `len(paths)` with a literal `3` — the fixture's own count — and the entire
    API suite stayed green, so the criterion "the quarantine count is real" was
    unpinned on the backend while the web tier pinned its half correctly.

    Two fixtures with different counts leave no constant that satisfies both.
    """
    _, session_id = make_session(client)
    _repository_with_planted_secrets(tmp_path, extra_secrets=extra_secrets)

    result = filter_tree(tmp_path)
    assert result.quarantined_paths, "the fixture planted no detectable secret"
    assert len(result.quarantined_paths) == 3 + extra_secrets, (
        "the fixture no longer quarantines a predictable number of files, so the "
        "parametrisation no longer varies the count and a constant could pass again"
    )

    index = RepositoryIndex(root=str(tmp_path), framework={"name": "next"})  # type: ignore[arg-type]
    payload = index.model_dump(mode="json") | {"quarantined_paths": result.quarantined_paths}
    store_analysis(client, session_id, payload)

    filtering = trust_state(client, session_id)["secret_filtering"]
    assert filtering["analyzed"] is True
    assert filtering["quarantined_count"] == len(result.quarantined_paths)
    assert sorted(filtering["quarantined_paths"]) == sorted(result.quarantined_paths)


def test_the_response_carries_paths_and_never_the_bytes_behind_them(
    client: TestClient, tmp_path: Path
) -> None:
    """`04_FRONTEND_SPEC.md` §8: paths only, never contents."""
    _, session_id = make_session(client)
    _repository_with_planted_secrets(tmp_path)
    result = filter_tree(tmp_path)
    store_analysis(client, session_id, {"quarantined_paths": result.quarantined_paths})

    body = client.get(f"/api/sessions/{session_id}/trust", headers=auth()).text
    assert PLANTED_SECRET not in body
    assert "BEGIN PRIVATE KEY" not in body


def test_only_the_quarantine_field_is_copied_out_of_the_analysis_payload(
    client: TestClient,
) -> None:
    """A payload with other keys contributes none of them to this response."""
    _, session_id = make_session(client)
    store_analysis(
        client,
        session_id,
        {
            "quarantined_paths": [".env"],
            "file_contents": {"src/page.tsx": PLANTED_SECRET},
            "notes": PLANTED_SECRET,
        },
    )
    body = client.get(f"/api/sessions/{session_id}/trust", headers=auth()).text
    assert PLANTED_SECRET not in body
    assert trust_state(client, session_id)["secret_filtering"]["quarantined_paths"] == [".env"]


def test_a_malformed_quarantine_field_yields_no_paths() -> None:
    assert trust.quarantined_paths_of({"quarantined_paths": "src/.env"}) == []
    assert trust.quarantined_paths_of({"quarantined_paths": [".env", 7, "", None]}) == [".env"]
    assert trust.quarantined_paths_of({}) == []


def test_quarantine_field_is_the_repository_index_field() -> None:
    """The reader's key is the writer's field, so the two cannot drift apart."""
    assert trust.QUARANTINE_FIELD in RepositoryIndex.model_fields


def test_filtering_reports_inactive_when_the_rules_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`active` is derived from the loaded rules, not asserted.

    `classify_path` and `scan_content` read these same module globals, so a run
    with them emptied really would quarantine nothing — and this row says so
    instead of printing "Active".
    """
    monkeypatch.setattr(filters, "SECRET_PATH_PATTERNS", ())
    monkeypatch.setattr(filters, "SECRET_DIR_SEGMENTS", frozenset())
    monkeypatch.setattr(filters, "CONTENT_PATTERNS", ())

    state = trust.secret_filtering_state(None)
    assert state.active is False
    assert state.rule_count == 0


# -- secure execution -------------------------------------------------------


class ProviderStub:
    """A provider that reports whatever it is told to, so the endpoint's own
    handling of that report is what is under test."""

    def __init__(self, level: TrustLevel, evidence: AttestationEvidence | None) -> None:
        self._level = level
        self._evidence = evidence

    @property
    def trust_level(self) -> TrustLevel:
        return self._level

    async def create_workspace(self, spec: WorkspaceSpec) -> Workspace:  # pragma: no cover
        raise NotImplementedError

    async def run(  # pragma: no cover
        self, workspace: Workspace, command: Command
    ) -> CommandResult:
        raise NotImplementedError

    async def attestation(self) -> AttestationEvidence | None:
        return self._evidence

    async def destroy(self, workspace: Workspace) -> None:  # pragma: no cover
        raise NotImplementedError


def fabricated_evidence() -> AttestationEvidence:
    """Evidence no part of MCPForge can produce, built here to test pass-through.

    It exists so the endpoint is shown to report the provider's real level
    rather than being hardcoded to the unattested one — a hardcode would make
    the panel honest today and wrong the moment `F8-02` lands.
    """
    now = datetime.now(UTC)
    return AttestationEvidence(
        issuer="https://confidentialcomputing.googleapis.com",
        audience="mcpforge-run-1",
        subject="https://www.googleapis.com/compute/v1/projects/mcpforge-aa5c2/zones/us-central1-a/instances/x",
        image_digest="sha256:" + "a" * 64,
        image_reference="us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor/x",
        workload_service_account="mcpforge-workload@mcpforge-aa5c2.iam.gserviceaccount.com",
        hardware_model="GCP_AMD_SEV_SNP",
        software_name="CONFIDENTIAL_SPACE",
        debug_status="disabled",
        issued_at=now,
        expires_at=now,
        verified_at=now,
    )


def test_with_no_provider_the_row_says_so_and_claims_nothing(client: TestClient) -> None:
    _, session_id = make_session(client)
    execution = trust_state(client, session_id)["secure_execution"]

    assert execution["trust_level"] == "DEVELOPMENT_ISOLATION"
    assert execution["provider_running"] is False
    assert execution["evidence"] is None
    assert execution["detail"]
    assert execution["configured_executor"] == SecureExecutorKind.DEVELOPMENT.value


def test_the_development_executor_reports_development_isolation(
    settings: Settings, tmp_path: Path
) -> None:
    """The real executor, not a stub: this is what runs today."""
    executor = DevelopmentSecureExecutor(workspace_root=tmp_path / "ws")
    with build_client(settings, executor=executor) as client:
        _, session_id = make_session(client)
        execution = trust_state(client, session_id)["secure_execution"]

    assert execution["trust_level"] == "DEVELOPMENT_ISOLATION"
    assert execution["provider_running"] is True
    assert execution["evidence"] is None


def test_a_provider_claiming_attestation_without_evidence_is_not_believed(
    settings: Settings,
) -> None:
    """The invariant lives in `AttestationOutcome`; this endpoint reuses it.

    A provider that returns the attested level while `attestation()` gives
    nothing is reported as unattested. There is no second copy of that rule
    here — `AttestationOutcome.rejected` is the same call every failed
    verification makes.
    """
    stub = ProviderStub(TrustLevel.HARDWARE_ATTESTED, None)
    with build_client(settings, executor=stub) as client:
        _, session_id = make_session(client)
        execution = trust_state(client, session_id)["secure_execution"]

    assert execution["trust_level"] == "DEVELOPMENT_ISOLATION"
    assert execution["evidence"] is None


def test_the_row_is_not_hardcoded_to_the_unattested_level(settings: Settings) -> None:
    """With real evidence present the level passes through unchanged.

    See `fabricated_evidence`: the evidence is the test's, not the product's.
    """
    stub = ProviderStub(TrustLevel.HARDWARE_ATTESTED, fabricated_evidence())
    with build_client(settings, executor=stub) as client:
        _, session_id = make_session(client)
        execution = trust_state(client, session_id)["secure_execution"]

    assert execution["trust_level"] == "HARDWARE_ATTESTED"
    assert execution["evidence"]["hardware_model"] == "GCP_AMD_SEV_SNP"
    assert execution["evidence"]["image_digest"] == "sha256:" + "a" * 64


def test_evidence_without_a_raised_level_is_reported_as_the_provider_gave_it(
    settings: Settings,
) -> None:
    """No upgrade is inferred from the presence of evidence alone."""
    stub = ProviderStub(TrustLevel.DEVELOPMENT_ISOLATION, fabricated_evidence())
    with build_client(settings, executor=stub) as client:
        _, session_id = make_session(client)
        execution = trust_state(client, session_id)["secure_execution"]

    assert execution["trust_level"] == "DEVELOPMENT_ISOLATION"
    assert "did not raise" in execution["detail"]


# -- branch protection ------------------------------------------------------


def test_branch_protection_describes_the_shape_the_writer_actually_enforces(
    client: TestClient,
) -> None:
    """The shipped pattern is exercised, not compared to a copy of itself.

    A branch the writer would create must match it, the default branch must not,
    and the traversal string that defeated a prefix check in Phase 6 must not.
    """
    _, session_id = make_session(client)
    protection = trust_state(client, session_id)["branch_protection"]

    shape = re.compile(protection["branch_shape"])
    assert shape.match(branch_name_for("hotel search"))
    assert not shape.match("main")
    assert not shape.match("mcpforge/../../../other")
    assert protection["branch_prefix"] == BRANCH_PREFIX
    assert "main" in protection["protected_names"]


# -- what the server deliberately does not answer ---------------------------


def test_the_server_makes_no_claim_about_the_browsers_webmcp_support(
    client: TestClient,
) -> None:
    """Adapter state is a fact about the browser. A server answer would be a guess."""
    _, session_id = make_session(client)
    body = trust_state(client, session_id)
    assert not [key for key in body if "webmcp" in key or "adapter" in key]
