"""Ingestion sources — F3-05/F3-07, 02_ARCHITECTURE.md §5.

Both sources converge before any filtering, so there is exactly one analysis
pipeline. That is the property that makes the demo project a real rehearsal
rather than a separate track, so it is asserted rather than assumed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcpforge.execution.development import DevelopmentSecureExecutor
from mcpforge.execution.provider import CommandResult, SandboxError, WorkspaceSpec
from mcpforge.github.client import InstallationToken
from mcpforge.indexing.indexer import build_index
from mcpforge.indexing.sources import (
    CHECKOUT_DIR,
    DemoSource,
    GitHubSource,
    IngestionError,
    RepositorySource,
)

DEMO = Path(__file__).resolve().parents[3] / "fixtures" / "demo-hotel-app"


@pytest.fixture
def executor(tmp_path: Path) -> DevelopmentSecureExecutor:
    return DevelopmentSecureExecutor(workspace_root=tmp_path / "ws", cpu_seconds=30)


def test_both_sources_satisfy_the_port() -> None:
    demo: RepositorySource = DemoSource(DEMO)
    github: RepositorySource = GitHubSource(
        full_name="o/r",
        branch="main",
        token=InstallationToken(token="t", expires_at=datetime.now(UTC) + timedelta(hours=1)),
    )
    assert demo.name == "demo"
    assert github.name == "github"


async def test_the_demo_source_lands_the_fixture_in_the_workspace(
    executor: DevelopmentSecureExecutor,
) -> None:
    async with executor.workspace(WorkspaceSpec(run_id="demo")) as ws:
        checkout = await DemoSource(DEMO).fetch(executor, ws)
        assert checkout.path == ws.root / CHECKOUT_DIR
        assert (checkout.path / "src" / "lib" / "reservations.ts").is_file()


async def test_the_demo_source_produces_the_same_index_as_the_fixture(
    executor: DevelopmentSecureExecutor,
) -> None:
    """One pipeline: what comes out of ingestion is indexed identically."""
    async with executor.workspace(WorkspaceSpec(run_id="demo")) as ws:
        checkout = await DemoSource(DEMO).fetch(executor, ws)
        ingested = build_index(checkout.path)

    direct = build_index(DEMO)
    assert {f.path for f in ingested.files} == {f.path for f in direct.files}
    assert ingested.framework.name == direct.framework.name
    assert ingested.framework.router == direct.framework.router


async def test_the_demo_source_adds_no_networked_operation(
    executor: DevelopmentSecureExecutor,
) -> None:
    """03_SECURITY_ACCESS.md §3 — the clone stays the only networked step."""
    async with executor.workspace(WorkspaceSpec(run_id="demo")) as ws:
        assert ws.allow_network is False
        await DemoSource(DEMO).fetch(executor, ws)


async def test_a_missing_fixture_is_reported_rather_than_silently_empty(
    executor: DevelopmentSecureExecutor, tmp_path: Path
) -> None:
    async with executor.workspace(WorkspaceSpec(run_id="demo")) as ws:
        with pytest.raises(IngestionError, match="not found"):
            await DemoSource(tmp_path / "absent").fetch(executor, ws)


async def test_cloning_without_network_permission_is_refused(
    executor: DevelopmentSecureExecutor,
) -> None:
    source = GitHubSource(
        full_name="o/r",
        branch="main",
        token=InstallationToken(token="t", expires_at=datetime.now(UTC) + timedelta(hours=1)),
    )
    async with executor.workspace(WorkspaceSpec(run_id="clone")) as ws:
        with pytest.raises(SandboxError, match="allow_network=True"):
            await source.fetch(executor, ws)


async def test_an_expired_token_is_refused_before_the_clone_starts(
    executor: DevelopmentSecureExecutor,
) -> None:
    source = GitHubSource(
        full_name="o/r",
        branch="main",
        token=InstallationToken(token="t", expires_at=datetime.now(UTC) - timedelta(minutes=1)),
    )
    async with executor.workspace(WorkspaceSpec(run_id="clone", allow_network=True)) as ws:
        with pytest.raises(IngestionError, match="expired"):
            await source.fetch(executor, ws)


async def test_a_failed_clone_never_leaks_the_token(
    executor: DevelopmentSecureExecutor,
) -> None:
    """git echoes the remote URL on failure, and the URL carries the token."""
    token = "ghs_a_very_secret_installation_token"
    source = GitHubSource(
        full_name="this-owner-does-not-exist-999/nope",
        branch="main",
        token=InstallationToken(token=token, expires_at=datetime.now(UTC) + timedelta(hours=1)),
    )
    async with executor.workspace(WorkspaceSpec(run_id="clone", allow_network=True)) as ws:
        with pytest.raises(IngestionError) as exc:
            await source.fetch(executor, ws)

    # The property that matters. Modern git strips credentials from its own
    # error text, so this passes for two reasons at once — which is why the
    # redaction itself is tested directly below rather than only here.
    assert token not in str(exc.value)


async def test_the_clone_error_redacts_a_token_that_git_does_echo(
    executor: DevelopmentSecureExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not every git version scrubs the remote URL, so we scrub it ourselves.

    Drives the redaction directly, because relying on git's own behaviour would
    make the test pass without our code doing anything.
    """
    token = "ghs_a_very_secret_installation_token"
    source = GitHubSource(
        full_name="o/r",
        branch="main",
        token=InstallationToken(token=token, expires_at=datetime.now(UTC) + timedelta(hours=1)),
    )

    async def leaky_run(workspace: object, command: object) -> CommandResult:
        return CommandResult(
            argv=("git", "clone"),
            exit_code=128,
            stdout="",
            stderr=f"fatal: could not read from https://x-access-token:{token}@github.com/o/r.git",
            duration_seconds=0.1,
        )

    monkeypatch.setattr(executor, "run", leaky_run)
    async with executor.workspace(WorkspaceSpec(run_id="clone", allow_network=True)) as ws:
        with pytest.raises(IngestionError) as exc:
            await source.fetch(executor, ws)

    assert token not in str(exc.value)
    assert "[redacted]" in str(exc.value)
