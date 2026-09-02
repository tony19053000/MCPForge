"""Repository ingestion sources — 02_ARCHITECTURE.md §5.

Two ways a repository reaches the pipeline, behind one port. They converge
**before any filtering**, so everything downstream — the path policy, the secret
filter, classification, parsing, the graph, retrieval — is a single code path.
There is exactly one analysis pipeline, and that is what makes the demo project
a genuine rehearsal rather than a separate track.

    GitHubSource ─┐
                  ├─→ workspace ─→ filter ─→ index
    DemoSource  ──┘
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import anyio.to_thread

from mcpforge.execution.provider import (
    Command,
    SandboxError,
    SecureExecutionProvider,
    Workspace,
)
from mcpforge.github.client import GitHubAppClient, InstallationToken
from mcpforge.logging import get_logger

log = get_logger(__name__)

#: Where a source always places the checkout inside the workspace.
CHECKOUT_DIR = "repo"


class IngestionError(Exception):
    """The repository could not be brought into the workspace."""


@dataclass(frozen=True)
class Checkout:
    """A repository sitting in the workspace, ready to filter and index."""

    path: Path
    source: str
    reference: str


class RepositorySource(Protocol):
    """How a repository gets into the workspace. Nothing more."""

    @property
    def name(self) -> str: ...

    async def fetch(self, executor: SecureExecutionProvider, workspace: Workspace) -> Checkout: ...


class DemoSource:
    """The bundled fixture application.

    A local copy, so it adds no networked operation — the clone remains the only
    one (03_SECURITY_ACCESS.md §3). A demo project has no bound repository id and
    therefore can never reach a write path.
    """

    def __init__(self, fixture_root: Path) -> None:
        self._fixture_root = fixture_root.resolve()

    @property
    def name(self) -> str:
        return "demo"

    async def fetch(self, executor: SecureExecutionProvider, workspace: Workspace) -> Checkout:
        if not self._fixture_root.is_dir():
            raise IngestionError(f"Demo fixture not found at {self._fixture_root}")

        destination = workspace.root / CHECKOUT_DIR

        def copy() -> None:
            shutil.copytree(
                self._fixture_root,
                destination,
                ignore=shutil.ignore_patterns("node_modules", ".next", ".git"),
            )

        await anyio.to_thread.run_sync(copy)
        log.info("ingestion.demo_copied", workspace_id=workspace.id)
        return Checkout(path=destination, source=self.name, reference="bundled-fixture")


class GitHubSource:
    """A shallow, single-branch clone of the bound repository.

    The installation token is used here and nowhere else, and never appears in a
    log or an error: git echoes the remote URL on failure, so stderr is redacted
    before it goes anywhere.
    """

    def __init__(
        self,
        *,
        full_name: str,
        branch: str,
        token: InstallationToken,
        client: GitHubAppClient | None = None,
    ) -> None:
        self._full_name = full_name
        self._branch = branch
        self._token = token
        self._client = client

    @property
    def name(self) -> str:
        return "github"

    async def fetch(self, executor: SecureExecutionProvider, workspace: Workspace) -> Checkout:
        if not workspace.allow_network:
            raise SandboxError(
                "Cloning needs the network. Create the workspace with "
                "WorkspaceSpec(allow_network=True) for the clone step only."
            )
        if self._token.expired:
            raise IngestionError("The installation token expired before the clone started.")

        url = f"https://x-access-token:{self._token.token}@github.com/{self._full_name}.git"
        result = await executor.run(
            workspace,
            Command(
                argv=(
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--single-branch",
                    "--branch",
                    self._branch,
                    url,
                    CHECKOUT_DIR,
                ),
                timeout_seconds=300,
            ),
        )
        if not result.ok:
            # git echoes the tokenised URL on failure. Redact before it escapes.
            detail = result.stderr.replace(self._token.token, "[redacted]")
            raise IngestionError(
                f"Clone of {self._full_name}@{self._branch} failed "
                f"(exit {result.exit_code}): {detail[-400:]}"
            )

        log.info("ingestion.cloned", repository=self._full_name, branch=self._branch)
        return Checkout(
            path=workspace.root / CHECKOUT_DIR, source=self.name, reference=self._branch
        )
