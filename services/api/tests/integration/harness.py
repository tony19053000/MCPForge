"""Shared driver for the F9-01 pipeline suites.

Both suites — the offline composition suite and the live legs — drive the
product the way a developer does: over HTTP, through the real routes, with a
real RS256 bearer token checked by the real `FirebaseIdTokenVerifier`.

**The identity.** The token is signed by a key generated for the test session
(`tests/conftest.py`), and the verifier is handed a JWKS client that serves that
key's public half in place of Google's endpoint. Signature, issuer, audience,
expiry and subject are all genuinely verified. Nothing here is reachable from
production code: `create_app` takes a verifier as an argument, and the stub JWKS
client lives in the test tree. There is no bypass flag, test mode or alternate
verifier in `src/`.

**Approvals.** Every decision is recorded through `POST /api/approvals/{id}/decide`
— the one decision endpoint — never by writing a record into the store.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from mcpforge.models.transitions import LEGAL

REPO_ROOT = Path(__file__).resolve().parents[4]
DEMO_APP = REPO_ROOT / "fixtures" / "demo-hotel-app"
NODE_MODULES = REPO_ROOT / "node_modules"

#: Every edge in the transition table, as (from, to) value pairs.
ALL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    (source.value, target.value) for source, targets in LEGAL.items() for target in targets
)

#: The analysis leg's exact path. Nothing past VALIDATION_PASSED.
DEMO_PATH: tuple[str, ...] = (
    "PROJECT_CREATED",
    "ANALYSIS_PENDING",
    "ANALYSIS_RUNNING",
    "ANALYSIS_COMPLETE",
    "WORKFLOW_SELECTION_PENDING",
    "WORKFLOWS_SELECTED",
    "TOOL_PLAN_RUNNING",
    "TOOL_PLAN_READY",
    "TOOL_PLAN_APPROVAL_PENDING",
    "TOOL_PLAN_APPROVED",
    "GENERATION_RUNNING",
    "PATCH_READY",
    "SECURITY_REVIEW_RUNNING",
    "SECURITY_REVIEW_PASSED",
    "PATCH_APPROVAL_PENDING",
    "PATCH_APPROVED",
    "VALIDATION_RUNNING",
    "VALIDATION_PASSED",
)

PR_STATES = frozenset(
    {"PR_APPROVAL_PENDING", "PR_APPROVED", "PR_CREATING", "PR_CREATED", "COMPLETE"}
)


class Driver:
    """Drives one app as one signed-in developer."""

    def __init__(self, client: TestClient, token: str) -> None:
        self.client = client
        self._headers = {"Authorization": f"Bearer {token}"}

    # -- raw ----------------------------------------------------------------

    def post(self, path: str, json: dict[str, Any] | None = None) -> httpx.Response:
        response: httpx.Response = self.client.post(path, json=json, headers=self._headers)
        return response

    def get(self, path: str) -> httpx.Response:
        response: httpx.Response = self.client.get(path, headers=self._headers)
        return response

    # -- the product's own routes -------------------------------------------

    def create_project(self, name: str) -> str:
        response = self.post("/api/projects", {"name": name})
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def create_session(self, project_id: str) -> str:
        response = self.post(f"/api/projects/{project_id}/sessions")
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    def step(
        self, session_id: str, stage: str, json: dict[str, Any] | None = None, *, expect: int = 200
    ) -> dict[str, Any]:
        """One pipeline stage. Asserts the status code the caller expects."""
        response = self.post(f"/api/sessions/{session_id}/pipeline/{stage}", json)
        assert response.status_code == expect, f"{stage}: {response.status_code} {response.text}"
        body: dict[str, Any] = response.json()
        return body

    def agent(
        self, session_id: str, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = self.post(f"/api/agent/sessions/{session_id}/{path}", json)
        assert response.status_code == 200, f"agent {path}: {response.text}"
        body: dict[str, Any] = response.json()
        return body

    def agent_read(self, session_id: str, path: str) -> dict[str, Any]:
        response = self.get(f"/api/agent/sessions/{session_id}/{path}")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def decide(self, approval_id: str, decision: str = "APPROVED") -> dict[str, Any]:
        """The human decision, through the real endpoint and nothing else."""
        response = self.post(f"/api/approvals/{approval_id}/decide", {"decision": decision})
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        assert body["status"] == decision
        return body

    # -- reading back what was stored ----------------------------------------

    def stored_state(self, session_id: str, owner: str) -> str:
        """The state in the store, not in a response body."""
        store = self.client.app.state.store  # type: ignore[attr-defined]
        return str(asyncio.run(store.get_session(session_id, owner)).state.value)

    def transitions(self, session_id: str) -> list[tuple[str, str]]:
        """Every transition the machine recorded for this session, in order."""
        events = self.get(f"/api/sessions/{session_id}/events").json()
        return [
            (e["detail"]["from"], e["detail"]["to"]) for e in events if e["kind"] == "state.changed"
        ]

    def events(self, session_id: str) -> list[dict[str, Any]]:
        body: list[dict[str, Any]] = self.get(f"/api/sessions/{session_id}/events").json()
        return body


def assert_the_writer_refuses_a_demo_run(
    client: TestClient, session_id: str, owner: str, repository_full_name: str
) -> str:
    """Hand the writer everything a demo run produced, and require a refusal.

    Built from the run's own stored artifacts and its real PATCH approval, so
    the only thing missing is what a demo project can never have: a bound
    repository. The refusal must come from the boundary, before any request is
    made — a recorded transport proves not one call reached GitHub.
    """
    from datetime import UTC, datetime, timedelta

    from mcpforge.generation.nextjs import generate_patch
    from mcpforge.github.boundary import NoRepositoryBoundError
    from mcpforge.github.branches import branch_name_for
    from mcpforge.github.client import InstallationToken
    from mcpforge.github.writer import BranchAndPullRequestWriter
    from mcpforge.models.core import ApprovalGate, ArtifactKind, artifact_hash
    from mcpforge.models.index import RepositoryIndex
    from mcpforge.models.toolplan import ToolPlan
    from mcpforge.orchestration.toolset import toolset_from_plan

    store = client.app.state.store  # type: ignore[attr-defined]
    session = asyncio.run(store.get_session(session_id, owner))
    project = asyncio.run(store.get_project(session.project_id, owner))
    assert project.is_demo, "this check is about a demo project"

    analysis = asyncio.run(store.get_artifact(session_id, ArtifactKind.ANALYSIS, owner))
    plan_artifact = asyncio.run(store.get_artifact(session_id, ArtifactKind.TOOL_PLAN, owner))
    patch_artifact = asyncio.run(store.get_artifact(session_id, ArtifactKind.PATCH, owner))
    assert analysis and plan_artifact and patch_artifact, "the demo run stored no artifacts"

    plan = ToolPlan.model_validate(plan_artifact.payload["plan"])
    index = RepositoryIndex.model_validate(analysis.payload["index"])
    patch = generate_patch(toolset_from_plan(plan, index), base_commit=None)
    assert artifact_hash(patch.hashable()) == patch_artifact.hash
    patch_approval = asyncio.run(
        store.find_approval(session_id, ApprovalGate.PATCH, patch_artifact.hash, owner)
    )
    assert patch_approval is not None, "the demo run never had its patch approved"

    calls: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(500)

    writer = BranchAndPullRequestWriter(
        token=InstallationToken(
            token="never-sent", expires_at=datetime.now(UTC) + timedelta(hours=1)
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )
    try:
        asyncio.run(
            writer.create_pull_request(
                project=project,
                repository_full_name=repository_full_name,
                default_branch="main",
                base_commit="0" * 40,
                branch=branch_name_for(session_id),
                patch=patch,
                plan=plan,
                patch_approval=patch_approval,
                pr_approval=patch_approval,
                session_id=session_id,
            )
        )
    except NoRepositoryBoundError as refusal:
        assert calls == [], f"the writer reached GitHub before refusing: {calls}"
        return str(refusal)
    raise AssertionError("the PR writer accepted a demo project")


def path_of(transitions: list[tuple[str, str]]) -> tuple[str, ...]:
    """The states a run visited, starting from PROJECT_CREATED."""
    if not transitions:
        return ("PROJECT_CREATED",)
    visited = [transitions[0][0]]
    for source, target in transitions:
        assert source == visited[-1], f"discontinuous transitions at {source} -> {target}"
        visited.append(target)
    return tuple(visited)
