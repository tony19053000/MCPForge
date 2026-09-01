"""GitHub App client — 03_SECURITY_ACCESS.md §6.

Access is scoped by construction:

- A **GitHub App**, not an OAuth token for the whole account.
- Installation access tokens are minted per operation, expire in an hour, and
  are never persisted.
- The private key lives outside the repository, is read at use time, and never
  reaches a log or a response.
- MCPForge identity (Firebase) is separate from repository authorization (this).
  Signing in grants neither, and this module knows nothing about users.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt

from mcpforge.logging import get_logger

log = get_logger(__name__)

GITHUB_API = "https://api.github.com"
ACCEPT = "application/vnd.github+json"

#: An App JWT is short-lived by design. GitHub rejects anything over 10 minutes.
APP_JWT_TTL_SECONDS = 540


class GitHubError(Exception):
    """A GitHub call failed. Always typed, never a bare exception."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitHubNotConfiguredError(GitHubError):
    """No App credentials. Distinct from a failed call, so the product can say
    'not configured' rather than implying GitHub refused."""


@dataclass(frozen=True)
class Installation:
    id: int
    account: str
    #: "selected" means specific repositories; "all" would be account-wide.
    repository_selection: str

    @property
    def is_scoped_to_selected_repositories(self) -> bool:
        return self.repository_selection == "selected"


@dataclass(frozen=True)
class Repository:
    id: int
    full_name: str
    default_branch: str
    private: bool

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1]


@dataclass(frozen=True)
class InstallationToken:
    """Short-lived, never persisted, never logged."""

    token: str
    expires_at: datetime

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    def __repr__(self) -> str:
        return f"InstallationToken(expires_at={self.expires_at.isoformat()}, token=[redacted])"


class GitHubAppClient:
    """Talks to GitHub as the App, and as an installation of the App."""

    def __init__(
        self,
        *,
        app_id: str | None,
        private_key_path: str | None,
        http: httpx.AsyncClient | None = None,
        base_url: str = GITHUB_API,
    ) -> None:
        self._app_id = app_id
        self._private_key_path = private_key_path
        self._http = http
        self._base_url = base_url.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self._app_id and self._private_key_path)

    def _require_config(self) -> tuple[str, str]:
        if not self._app_id or not self._private_key_path:
            raise GitHubNotConfiguredError(
                "GitHub is not configured: set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH."
            )
        return self._app_id, self._private_key_path

    def _read_private_key(self) -> str:
        _, path = self._require_config()
        key_file = Path(path)
        if not key_file.is_file():
            raise GitHubNotConfiguredError(
                f"GitHub App private key not found at {path}. It must live outside "
                "the repository and never be committed."
            )
        return key_file.read_text()

    def app_jwt(self) -> str:
        """Mint a short-lived JWT signed by the App's private key.

        Not cached: minting is cheap, and a cached JWT is one more secret held
        in memory for longer than it needs to be.
        """
        app_id, _ = self._require_config()
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + APP_JWT_TTL_SECONDS, "iss": app_id},
            self._read_private_key(),
            algorithm="RS256",
        )

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30)
        return self._http

    async def _get(self, path: str, token: str) -> httpx.Response:
        client = await self._client()
        response = await client.get(
            f"{self._base_url}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT},
        )
        if response.status_code >= 400:
            # The message names the path and status only. The token is never
            # echoed, and neither is the response body.
            raise GitHubError(
                f"GET {path} failed: {response.status_code}", status=response.status_code
            )
        return response

    async def list_installations(self) -> list[Installation]:
        response = await self._get("/app/installations", self.app_jwt())
        return [
            Installation(
                id=item["id"],
                account=item["account"]["login"],
                repository_selection=item.get("repository_selection", "selected"),
            )
            for item in response.json()
        ]

    async def create_installation_token(self, installation_id: int) -> InstallationToken:
        """Mint a token for one installation. Short-lived and never stored."""
        client = await self._client()
        response = await client.post(
            f"{self._base_url}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self.app_jwt()}", "Accept": ACCEPT},
        )
        if response.status_code >= 400:
            raise GitHubError(
                f"Could not mint an installation token: {response.status_code}",
                status=response.status_code,
            )
        body = response.json()
        log.info("github.installation_token_minted", installation_id=installation_id)
        return InstallationToken(
            token=body["token"],
            expires_at=datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")),
        )

    async def list_repositories(self, token: InstallationToken) -> list[Repository]:
        """Only what this installation was granted. Never the whole account."""
        response = await self._get("/installation/repositories", token.token)
        return [
            Repository(
                id=item["id"],
                full_name=item["full_name"],
                default_branch=item["default_branch"],
                private=item["private"],
            )
            for item in response.json().get("repositories", [])
        ]

    async def get_repository(self, token: InstallationToken, full_name: str) -> Repository:
        response = await self._get(f"/repos/{full_name}", token.token)
        item = response.json()
        return Repository(
            id=item["id"],
            full_name=item["full_name"],
            default_branch=item["default_branch"],
            private=item["private"],
        )

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
