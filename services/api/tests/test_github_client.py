"""GitHub App client — F3-01. The T9 control: scoped access, short-lived tokens."""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mcpforge.github.client import (
    GitHubAppClient,
    GitHubError,
    GitHubNotConfiguredError,
    InstallationToken,
)

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
APP_ID = "4797679"


@pytest.fixture(scope="session")
def app_key(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path_factory.mktemp("keys") / "app.pem"
    path.write_bytes(pem)
    return path


def make_client(app_key: pathlib.Path, handler: object) -> GitHubAppClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return GitHubAppClient(
        app_id=APP_ID,
        private_key_path=str(app_key),
        http=httpx.AsyncClient(transport=transport),
        base_url="https://api.github.test",
    )


# -- configuration ---------------------------------------------------------


def test_an_unconfigured_client_refuses_rather_than_guessing() -> None:
    client = GitHubAppClient(app_id=None, private_key_path=None)
    assert client.configured is False
    with pytest.raises(GitHubNotConfiguredError, match="not configured"):
        client.app_jwt()


def test_a_missing_key_file_names_the_path_and_the_rule(tmp_path: pathlib.Path) -> None:
    client = GitHubAppClient(app_id=APP_ID, private_key_path=str(tmp_path / "absent.pem"))
    with pytest.raises(GitHubNotConfiguredError, match="never be committed"):
        client.app_jwt()


# -- the App JWT -----------------------------------------------------------


def test_the_app_jwt_is_signed_and_short_lived(app_key: pathlib.Path) -> None:
    client = GitHubAppClient(app_id=APP_ID, private_key_path=str(app_key))
    claims = jwt.decode(client.app_jwt(), options={"verify_signature": False})
    assert claims["iss"] == APP_ID
    lifetime = claims["exp"] - claims["iat"]
    # GitHub rejects anything over 10 minutes; staying well under is the point.
    assert lifetime <= 600


def test_the_jwt_is_rs256_not_a_symmetric_algorithm(app_key: pathlib.Path) -> None:
    client = GitHubAppClient(app_id=APP_ID, private_key_path=str(app_key))
    assert jwt.get_unverified_header(client.app_jwt())["alg"] == "RS256"


def test_each_call_mints_a_fresh_jwt_rather_than_caching_one(
    app_key: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached JWT is one more secret held longer than it needs to be."""
    client = GitHubAppClient(app_id=APP_ID, private_key_path=str(app_key))
    first = jwt.decode(client.app_jwt(), options={"verify_signature": False})
    monkeypatch.setattr("mcpforge.github.client.time.time", lambda: first["iat"] + 120)
    second = jwt.decode(client.app_jwt(), options={"verify_signature": False})
    assert second["iat"] > first["iat"]


# -- installation tokens ---------------------------------------------------


async def test_an_installation_token_is_minted_and_expires(app_key: pathlib.Path) -> None:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/99/access_tokens"
        assert request.method == "POST"
        return httpx.Response(201, json={"token": "ghs_installation_token", "expires_at": expires})

    token = await make_client(app_key, handler).create_installation_token(99)
    assert token.token == "ghs_installation_token"
    assert token.expired is False


async def test_an_expired_token_reports_itself_expired() -> None:
    token = InstallationToken(token="stale", expires_at=datetime.now(UTC) - timedelta(minutes=1))
    assert token.expired is True


def test_a_token_never_renders_itself_in_a_repr() -> None:
    """Tokens end up in tracebacks and logs. This one does not carry its value."""
    token = InstallationToken(token="ghs_secret_value", expires_at=datetime.now(UTC))
    assert "ghs_secret_value" not in repr(token)
    assert "[redacted]" in repr(token)


def test_tokens_are_never_persisted_anywhere() -> None:
    """03_SECURITY_ACCESS.md §6 — minted per operation, not stored."""
    offenders: list[str] = []
    for path in (SRC / "mcpforge").rglob("*.py"):
        text = path.read_text()
        if "InstallationToken" in text and ("store" in text.lower() and "save" in text.lower()):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, f"installation token may be persisted in: {offenders}"


# -- scoping, the T9 control ----------------------------------------------


async def test_only_installation_scoped_repositories_are_listed(app_key: pathlib.Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/installation/repositories"
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "repositories": [
                    {
                        "id": 1,
                        "full_name": "tony19053000/mcpforge-test",
                        "default_branch": "main",
                        "private": False,
                    }
                ],
            },
        )

    token = InstallationToken(token="t", expires_at=datetime.now(UTC) + timedelta(hours=1))
    repos = await make_client(app_key, handler).list_repositories(token)
    assert [r.full_name for r in repos] == ["tony19053000/mcpforge-test"]
    assert repos[0].owner == "tony19053000"
    assert repos[0].name == "mcpforge-test"


async def test_an_installation_reports_whether_it_is_repository_scoped(
    app_key: pathlib.Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "account": {"login": "tony19053000"},
                    "repository_selection": "selected",
                },
                {"id": 2, "account": {"login": "someorg"}, "repository_selection": "all"},
            ],
        )

    installations = await make_client(app_key, handler).list_installations()
    assert installations[0].is_scoped_to_selected_repositories is True
    assert installations[1].is_scoped_to_selected_repositories is False


async def test_the_app_endpoint_is_never_called_with_an_installation_token(
    app_key: pathlib.Path,
) -> None:
    """App-level endpoints use the App JWT; repository endpoints use the
    installation token. Mixing them would widen scope."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["authorization"]))
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"repositories": []})

    client = make_client(app_key, handler)
    await client.list_installations()
    token = InstallationToken(token="ghs_inst", expires_at=datetime.now(UTC) + timedelta(hours=1))
    await client.list_repositories(token)

    app_call = next(a for p, a in seen if p == "/app/installations")
    repo_call = next(a for p, a in seen if p == "/installation/repositories")
    assert "ghs_inst" not in app_call
    assert repo_call == "Bearer ghs_inst"


# -- errors ----------------------------------------------------------------


async def test_a_failed_call_raises_a_typed_error_with_its_status(
    app_key: pathlib.Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    token = InstallationToken(token="t", expires_at=datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(GitHubError) as exc:
        await make_client(app_key, handler).get_repository(token, "someone/private")
    assert exc.value.status == 404


async def test_an_error_message_never_echoes_the_token(app_key: pathlib.Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    token = InstallationToken(
        token="ghs_super_secret", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    with pytest.raises(GitHubError) as exc:
        await make_client(app_key, handler).list_repositories(token)
    assert "ghs_super_secret" not in str(exc.value)


async def test_a_token_mint_failure_is_reported_clearly(app_key: pathlib.Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    with pytest.raises(GitHubError, match="installation token"):
        await make_client(app_key, handler).create_installation_token(1)


# -- separation from user identity ----------------------------------------


def test_the_github_client_knows_nothing_about_mcpforge_users() -> None:
    """03_SECURITY_ACCESS.md §6 — signing in is not repository authorization."""
    text = (SRC / "mcpforge" / "github" / "client.py").read_text()
    for term in ("firebase", "VerifiedIdentity", "owner_uid", "TokenVerifier"):
        assert term not in text, f"github client references user identity: {term}"
