"""Repository binding and access elevation over HTTP — F3-02.

The boundary helpers are well tested on their own. These tests cover the routes
that must *call* them: a helper nobody reaches enforces nothing, and three
mutations survived here before this file existed — including unmounting the
entire router.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mcpforge.auth.identity import AuthError, VerifiedIdentity
from mcpforge.config import Settings
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.github.client import GitHubAppClient
from mcpforge.main import create_app
from mcpforge.store.memory import InMemoryStore
from mcpforge.store.port import NotFoundError

OWNER = "uid-owner"
OTHER = "uid-stranger"

REPO = {
    "repository_id": "12345",
    "full_name": "tony19053000/mcpforge-test",
    "base_branch": "main",
}


class TokenIsUid:
    async def verify(self, raw_token: str) -> VerifiedIdentity:
        if not raw_token.startswith("uid-"):
            raise AuthError("bad token")
        return VerifiedIdentity(subject=raw_token, issuer="test")


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def client(settings: Settings, store: InMemoryStore) -> Iterator[TestClient]:
    app = create_app(
        settings,
        token_verifier=TokenIsUid(),
        store=store,
        gemini=FakeGeminiProvider([]),
        # Unconfigured on purpose: the listing route must report that honestly.
        github=GitHubAppClient(app_id=None, private_key_path=None),
    )
    with TestClient(app) as c:
        yield c


def auth(uid: str = OWNER) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def make_project(client: TestClient, uid: str = OWNER) -> str:
    return str(client.post("/api/projects", json={"name": "hotel"}, headers=auth(uid)).json()["id"])


# -- the routes exist and are mounted --------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/github/repositories"),
        ("POST", "/api/projects/proj_x/repository"),
        ("POST", "/api/projects/proj_x/access/elevate"),
        ("POST", "/api/projects/proj_x/access/revoke"),
    ],
)
def test_every_repository_route_is_mounted_and_requires_authentication(
    client: TestClient, method: str, path: str
) -> None:
    """Unmounting the router was a surviving mutation: the whole API surface
    could vanish and nothing noticed."""
    response = client.request(method, path, json={})
    assert response.status_code != 404, f"{method} {path} is not mounted"
    assert response.status_code == 401


# -- binding ----------------------------------------------------------------


def test_binding_records_the_repository_and_stays_read_only(client: TestClient) -> None:
    project_id = make_project(client)
    body = client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth()).json()
    assert body["repository_full_name"] == REPO["full_name"]
    assert body["base_branch"] == "main"
    assert body["access_mode"] == "READ_ONLY"


def test_rebinding_to_a_different_repository_is_refused(client: TestClient) -> None:
    """F3-02 exists to prevent exactly this. The route must call the helper."""
    project_id = make_project(client)
    client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth())

    response = client.post(
        f"/api/projects/{project_id}/repository",
        json={"repository_id": "99999", "full_name": "someone/private", "base_branch": "main"},
        headers=auth(),
    )
    assert response.status_code == 409
    assert "already bound" in response.text


def test_rebinding_the_same_repository_is_idempotent(client: TestClient) -> None:
    project_id = make_project(client)
    client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth())
    again = client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth())
    assert again.status_code == 200


def test_a_stranger_cannot_bind_your_project(client: TestClient) -> None:
    project_id = make_project(client)
    response = client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth(OTHER))
    assert response.status_code == 404


# -- elevation --------------------------------------------------------------


def test_elevation_records_the_actor_from_the_verified_token(client: TestClient) -> None:
    """A surviving mutation replaced the token subject with the project's own
    owner field, severing the actor-from-token rule with nothing noticing."""
    project_id = make_project(client)
    client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth())

    body = client.post(f"/api/projects/{project_id}/access/elevate", headers=auth()).json()
    assert body["access_mode"] == "WRITE_PR"
    assert body["elevated_by"] == OWNER
    assert body["elevated_at"] is not None


def test_a_demo_project_can_never_be_elevated(client: TestClient) -> None:
    """No bound repository, so no write path, ever."""
    project_id = make_project(client)
    response = client.post(f"/api/projects/{project_id}/access/elevate", headers=auth())
    assert response.status_code == 409
    assert "permanently unable" in response.text


def test_a_stranger_cannot_elevate_your_project(client: TestClient) -> None:
    project_id = make_project(client)
    client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth())
    response = client.post(f"/api/projects/{project_id}/access/elevate", headers=auth(OTHER))
    assert response.status_code == 404


def test_revoking_returns_to_read_only_and_keeps_the_record(client: TestClient) -> None:
    project_id = make_project(client)
    client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth())
    client.post(f"/api/projects/{project_id}/access/elevate", headers=auth())

    body = client.post(f"/api/projects/{project_id}/access/revoke", headers=auth()).json()
    assert body["access_mode"] == "READ_ONLY"
    assert body["elevated_by"] == OWNER, "the audit trail must survive revocation"


def test_elevation_persists(client: TestClient, store: InMemoryStore) -> None:
    """Not just returned in the response — actually written to the store."""
    project_id = make_project(client)
    client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth())
    client.post(f"/api/projects/{project_id}/access/elevate", headers=auth())

    stored = client.get(f"/api/projects/{project_id}", headers=auth()).json()
    assert stored["access_mode"] == "WRITE_PR"


# -- honest reporting -------------------------------------------------------


def test_an_unconfigured_github_reports_unconfigured_rather_than_failing_oddly(
    client: TestClient,
) -> None:
    response = client.get("/api/github/repositories", headers=auth())
    assert response.status_code == 503
    assert "not configured" in response.text


class PermissiveStore(InMemoryStore):
    """A store whose ownership check has been defeated.

    Simulates a bug, or a future shared-project model, in which `get_project`
    hands back a project the caller does not own. The elevation route must still
    refuse, because it derives the actor from the verified token rather than
    from the project it was handed.

    Without this, replacing `actor_uid=identity.subject` with
    `actor_uid=project.owner_uid` is invisible: in every ordinary test those two
    values are equal by construction.
    """

    async def get_project(self, project_id: str, owner_uid: str):  # type: ignore[no-untyped-def]
        for project in self._projects.values():
            if project.id == project_id:
                return project.model_copy()
        raise NotFoundError(project_id)


def test_elevation_refuses_when_the_actor_is_not_the_owner(settings: Settings) -> None:
    """Defence in depth: the actor comes from the token, not from the project."""
    store = PermissiveStore()
    app = create_app(
        settings,
        token_verifier=TokenIsUid(),
        store=store,
        gemini=FakeGeminiProvider([]),
        github=GitHubAppClient(app_id=None, private_key_path=None),
    )
    with TestClient(app) as client:
        project_id = make_project(client, OWNER)
        client.post(f"/api/projects/{project_id}/repository", json=REPO, headers=auth(OWNER))

        # The store no longer filters by owner, so the stranger's request reaches
        # the elevation logic. It must still be refused.
        response = client.post(f"/api/projects/{project_id}/access/elevate", headers=auth(OTHER))
        assert response.status_code == 403, (
            "elevation used the project's owner instead of the verified token subject"
        )
        assert "project owner" in response.text


def test_the_supported_frameworks_endpoint_reports_the_registry(client: TestClient) -> None:
    """01_PRD.md §9 — the product states support from the registry, never from
    copy someone wrote once."""
    from mcpforge.generation.adapters.registry import ADAPTERS

    body = client.get("/api/frameworks").json()
    assert [f["framework"] for f in body] == [a.info.framework for a in ADAPTERS]
    assert [f["display_name"] for f in body] == [a.info.display_name for a in ADAPTERS]


def test_the_frameworks_endpoint_is_public(client: TestClient) -> None:
    """It says what the product can do; there is nothing private about it."""
    assert client.get("/api/frameworks").status_code == 200
