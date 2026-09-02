"""Development secure executor — F3-04.

The T6/T7 controls. Escape attempts must be rejected, limits must actually bite,
and nothing here may ever claim hardware attestation.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import signal
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from mcpforge.execution.development import DevelopmentSecureExecutor
from mcpforge.execution.provider import (
    Command,
    CommandNotAllowedError,
    PathEscapeError,
    SandboxError,
    SecureExecutionProvider,
    TrustLevel,
    Workspace,
    WorkspaceSpec,
)

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
async def executor(tmp_path: Path) -> DevelopmentSecureExecutor:
    return DevelopmentSecureExecutor(workspace_root=tmp_path / "workspaces", cpu_seconds=10)


@pytest.fixture
async def workspace(executor: DevelopmentSecureExecutor) -> AsyncIterator[Workspace]:
    ws = await executor.create_workspace(WorkspaceSpec(run_id="run1"))
    yield ws
    await executor.destroy(ws)


# -- honest trust reporting -------------------------------------------------


def test_the_executor_satisfies_the_port(executor: DevelopmentSecureExecutor) -> None:
    assert isinstance(executor, SecureExecutionProvider)


async def test_trust_level_is_development_isolation(executor: DevelopmentSecureExecutor) -> None:
    assert executor.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


async def test_no_attestation_is_invented(executor: DevelopmentSecureExecutor) -> None:
    """03_SECURITY_ACCESS.md §2 — never claim attestation without one."""
    assert await executor.attestation() is None


def test_hardware_attested_appears_only_as_an_enum_member() -> None:
    """No code path in the whole backend can produce HARDWARE_ATTESTED.

    03_SECURITY_ACCESS.md §2: it may be assigned only by code that has verified
    a real attestation, and no such code exists yet. If someone later wires it
    up optimistically — `return TrustLevel.HARDWARE_ATTESTED`, or assigning it
    behind a config flag — this fails.

    Uses the AST rather than text, so documentation explaining the rule does not
    trip it while real usage does.
    """
    import ast

    offenders: list[str] = []
    for path in (SRC / "mcpforge").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # TrustLevel.HARDWARE_ATTESTED used anywhere in real code.
            if isinstance(node, ast.Attribute) and node.attr == "HARDWARE_ATTESTED":
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}: attribute access")
            # A bare assignment outside the TrustLevel enum body.
            if isinstance(node, ast.ClassDef) and node.name != "TrustLevel":
                for stmt in ast.walk(node):
                    if isinstance(stmt, ast.Name) and stmt.id == "HARDWARE_ATTESTED":
                        offenders.append(f"{path.relative_to(SRC)}:{stmt.lineno}: assignment")

    assert not offenders, "HARDWARE_ATTESTED is reachable in code:\n" + "\n".join(offenders)


async def test_the_workspace_reports_its_own_trust_level(workspace: Workspace) -> None:
    assert workspace.trust_level is TrustLevel.DEVELOPMENT_ISOLATION


# -- path jail --------------------------------------------------------------


@pytest.mark.parametrize(
    "escape",
    ["..", "../..", "../../etc", "/etc/passwd", "../outside.txt", "sub/../../..", "/"],
)
async def test_paths_outside_the_workspace_are_rejected(
    executor: DevelopmentSecureExecutor, workspace: Workspace, escape: str
) -> None:
    with pytest.raises(PathEscapeError):
        executor.resolve_inside(workspace, escape)


async def test_a_symlink_pointing_out_is_rejected(
    executor: DevelopmentSecureExecutor, workspace: Workspace, tmp_path: Path
) -> None:
    """Symlinks are resolved before the check, so a link cannot smuggle a path out."""
    outside = tmp_path / "outside.txt"
    outside.write_text("data")
    (workspace.root / "link.txt").symlink_to(outside)

    with pytest.raises(PathEscapeError):
        executor.resolve_inside(workspace, "link.txt")


async def test_paths_inside_the_workspace_are_allowed(
    executor: DevelopmentSecureExecutor, workspace: Workspace
) -> None:
    (workspace.root / "src").mkdir()
    resolved = executor.resolve_inside(workspace, "src")
    assert resolved.is_relative_to(workspace.root)


# -- command allowlist ------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("sh", "-c", "echo hi"),
        ("bash", "-c", "cat /etc/passwd"),
        ("curl", "https://evil.test"),
        ("rm", "-rf", "/"),
        ("chmod", "777", "/"),
        ("ssh", "host"),
    ],
)
async def test_commands_outside_the_allowlist_are_refused(
    executor: DevelopmentSecureExecutor, workspace: Workspace, argv: tuple[str, ...]
) -> None:
    """No arbitrary shell, ever. Refused before a process is created."""
    with pytest.raises(CommandNotAllowedError):
        await executor.run(workspace, Command(argv=argv))


async def test_an_empty_command_is_refused(
    executor: DevelopmentSecureExecutor, workspace: Workspace
) -> None:
    with pytest.raises(CommandNotAllowedError):
        await executor.run(workspace, Command(argv=()))


def test_no_shell_string_interpolation_exists_anywhere() -> None:
    """03_SECURITY_ACCESS.md §3 — argument arrays only."""
    offenders: list[str] = []
    for path in (SRC / "mcpforge").rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "create_subprocess_shell" in code or "shell=True" in code:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}")
    assert not offenders, "shell execution found:\n" + "\n".join(offenders)


# -- real execution ---------------------------------------------------------


async def test_an_allowed_command_runs_and_reports_its_exit_code(
    executor: DevelopmentSecureExecutor, workspace: Workspace
) -> None:
    result = await executor.run(
        workspace, Command(argv=("python3", "-c", "print('hello from the sandbox')"))
    )
    assert result.ok
    assert result.exit_code == 0
    assert "hello from the sandbox" in result.stdout


async def test_a_failing_command_reports_a_nonzero_exit_code(
    executor: DevelopmentSecureExecutor, workspace: Workspace
) -> None:
    result = await executor.run(workspace, Command(argv=("python3", "-c", "raise SystemExit(3)")))
    assert result.exit_code == 3
    assert result.ok is False


async def test_a_command_runs_inside_the_workspace(
    executor: DevelopmentSecureExecutor, workspace: Workspace
) -> None:
    result = await executor.run(
        workspace, Command(argv=("python3", "-c", "import os; print(os.getcwd())"))
    )
    assert str(workspace.root) in result.stdout


async def test_the_parent_environment_does_not_leak_into_a_job(
    executor: DevelopmentSecureExecutor, workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository job must not be able to read our credentials."""
    monkeypatch.setenv("GEMINI_API_KEY", "AQ." + "should-never-be-visible-to-a-repo-job")
    result = await executor.run(
        workspace,
        Command(
            argv=("python3", "-c", "import os; print(os.environ.get('GEMINI_API_KEY','ABSENT'))")
        ),
    )
    assert "ABSENT" in result.stdout
    assert "should-never-be-visible" not in result.stdout


async def test_a_command_that_overruns_is_killed(
    executor: DevelopmentSecureExecutor, workspace: Workspace
) -> None:
    result = await executor.run(
        workspace,
        Command(argv=("python3", "-c", "import time; time.sleep(30)"), timeout_seconds=1),
    )
    assert result.timed_out is True
    assert result.ok is False


async def test_enormous_output_is_truncated(
    executor: DevelopmentSecureExecutor, workspace: Workspace
) -> None:
    result = await executor.run(
        workspace,
        Command(argv=("python3", "-c", "print('x' * 5_000_000)"), timeout_seconds=30),
    )
    assert result.output_truncated is True
    assert len(result.stdout) <= 1_000_000


# -- lifecycle --------------------------------------------------------------


async def test_a_workspace_is_private_to_its_owner(workspace: Workspace) -> None:
    assert oct(workspace.root.stat().st_mode)[-3:] == "700"


async def test_workspaces_are_isolated_from_each_other(
    executor: DevelopmentSecureExecutor,
) -> None:
    a = await executor.create_workspace(WorkspaceSpec(run_id="run-a"))
    b = await executor.create_workspace(WorkspaceSpec(run_id="run-b"))
    try:
        assert a.root != b.root
        (a.root / "secret.txt").write_text("a's data")
        assert not (b.root / "secret.txt").exists()
    finally:
        await executor.destroy(a)
        await executor.destroy(b)


async def test_destroy_removes_the_workspace(executor: DevelopmentSecureExecutor) -> None:
    ws = await executor.create_workspace(WorkspaceSpec(run_id="run-x"))
    (ws.root / "file.txt").write_text("data")
    await executor.destroy(ws)
    assert not ws.root.exists()


async def test_destroy_is_safe_to_call_twice(executor: DevelopmentSecureExecutor) -> None:
    """Cleanup must not fail on an error path where it has already run."""
    ws = await executor.create_workspace(WorkspaceSpec(run_id="run-y"))
    await executor.destroy(ws)
    await executor.destroy(ws)


# -- network isolation ------------------------------------------------------


async def test_a_job_is_denied_the_network_by_default(
    executor: DevelopmentSecureExecutor, workspace: Workspace
) -> None:
    """03_SECURITY_ACCESS.md §3 — no outbound network from analysis commands.

    Previously `allow_network` was declared and never read, so this control was
    documented in three places and enforced in none.
    """
    if not executor.network_isolation_available:
        pytest.skip("this machine cannot create unprivileged network namespaces")

    result = await executor.run(
        workspace,
        Command(
            argv=(
                "python3",
                "-c",
                "import socket\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
                "    print('REACHABLE')\n"
                "except OSError as e:\n"
                "    print('BLOCKED', e.errno)\n",
            ),
            timeout_seconds=20,
        ),
    )
    assert "REACHABLE" not in result.stdout
    assert "BLOCKED" in result.stdout


async def test_the_clone_step_may_have_the_network(
    executor: DevelopmentSecureExecutor,
) -> None:
    """Cloning is the one networked operation, so it must be expressible."""
    ws = await executor.create_workspace(WorkspaceSpec(run_id="net", allow_network=True))
    try:
        assert ws.allow_network is True
        result = await executor.run(
            ws,
            Command(
                argv=(
                    "python3",
                    "-c",
                    "import socket\n"
                    "try:\n"
                    "    socket.create_connection(('1.1.1.1', 53), timeout=5)\n"
                    "    print('REACHABLE')\n"
                    "except OSError:\n"
                    "    print('BLOCKED')\n",
                ),
                timeout_seconds=20,
            ),
        )
        assert "REACHABLE" in result.stdout
    finally:
        await executor.destroy(ws)


async def test_an_isolation_less_machine_is_refused_rather_than_quietly_downgraded(
    tmp_path: Path,
) -> None:
    """Running with network access while claiming isolation is the worse outcome."""
    executor = DevelopmentSecureExecutor(workspace_root=tmp_path / "ws")
    executor._network_isolation = False  # simulate a kernel without unprivileged userns
    ws = await executor.create_workspace(WorkspaceSpec(run_id="x"))
    try:
        with pytest.raises(SandboxError, match="cannot be denied the network"):
            await executor.run(ws, Command(argv=("python3", "-c", "pass")))
    finally:
        await executor.destroy(ws)


async def test_the_weaker_boundary_can_be_accepted_knowingly(tmp_path: Path) -> None:
    executor = DevelopmentSecureExecutor(
        workspace_root=tmp_path / "ws", require_network_isolation=False
    )
    executor._network_isolation = False
    ws = await executor.create_workspace(WorkspaceSpec(run_id="x"))
    try:
        result = await executor.run(ws, Command(argv=("python3", "-c", "print('ran')")))
        assert result.ok
    finally:
        await executor.destroy(ws)


# -- the wall clock, including forked children ------------------------------


async def test_a_forked_grandchild_is_killed_too_not_left_running(
    executor: DevelopmentSecureExecutor, workspace: Workspace
) -> None:
    """The wall clock must kill the whole process group.

    Killing only the direct child leaves grandchildren alive — git's https
    helper, npm, next all fork — so a timed-out job keeps consuming the machine
    and holding its output pipes. Asserting the grandchild is gone is the only
    way to tell the two apart; a run that merely *returns* looks identical.
    """
    pid_file = workspace.root / "grandchild.pid"
    result = await asyncio.wait_for(
        executor.run(
            workspace,
            Command(
                argv=(
                    "python3",
                    "-c",
                    "import subprocess, sys, os, time\n"
                    "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                    f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
                    "time.sleep(120)\n",
                ),
                timeout_seconds=3,
            ),
        ),
        timeout=40,
    )
    assert result.timed_out is True

    grandchild_pid = int(pid_file.read_text())
    await asyncio.sleep(0.5)
    still_alive = True
    try:
        os.kill(grandchild_pid, 0)
    except ProcessLookupError:
        still_alive = False
    except PermissionError:  # pragma: no cover - reparented but alive
        still_alive = True

    if still_alive:  # pragma: no cover - cleanup only on failure
        with contextlib.suppress(ProcessLookupError):
            os.kill(grandchild_pid, signal.SIGKILL)
    assert not still_alive, (
        f"grandchild {grandchild_pid} survived the timeout; the process group was not killed"
    )


# -- resource limits --------------------------------------------------------


async def test_the_memory_limit_is_enforced(tmp_path: Path) -> None:
    """A surviving mutation showed preexec_fn could be dropped entirely with no
    test noticing, while STATUS advertised memory limits."""
    executor = DevelopmentSecureExecutor(workspace_root=tmp_path / "ws", memory_mb=64)
    ws = await executor.create_workspace(WorkspaceSpec(run_id="mem"))
    try:
        result = await executor.run(
            ws,
            Command(
                argv=("python3", "-c", "x = bytearray(200 * 1024 * 1024); print('ALLOCATED')"),
                timeout_seconds=30,
            ),
        )
        assert "ALLOCATED" not in result.stdout
        assert result.ok is False
    finally:
        await executor.destroy(ws)


async def test_the_cpu_limit_is_enforced(tmp_path: Path) -> None:
    """Killed by RLIMIT_CPU, not by the wall clock: the timeout is far longer."""
    executor = DevelopmentSecureExecutor(workspace_root=tmp_path / "ws", cpu_seconds=1)
    ws = await executor.create_workspace(WorkspaceSpec(run_id="cpu"))
    try:
        result = await executor.run(
            ws,
            Command(argv=("python3", "-c", "\nwhile True:\n    pass\n"), timeout_seconds=60),
        )
        assert result.timed_out is False, "should die on CPU, not on the wall clock"
        assert result.ok is False
    finally:
        await executor.destroy(ws)


# -- lifecycle guarantees ---------------------------------------------------


async def test_the_workspace_context_manager_destroys_on_success(
    executor: DevelopmentSecureExecutor,
) -> None:
    async with executor.workspace(WorkspaceSpec(run_id="ctx-ok")) as ws:
        (ws.root / "file.txt").write_text("data")
        root = ws.root
    assert not root.exists()


async def test_the_workspace_is_destroyed_even_when_the_job_raises(
    executor: DevelopmentSecureExecutor,
) -> None:
    """The path that raises is exactly the one that would otherwise leave a
    checkout of someone's private source on the disk."""
    root: Path | None = None
    with pytest.raises(RuntimeError, match="analysis blew up"):
        async with executor.workspace(WorkspaceSpec(run_id="ctx-fail")) as ws:
            root = ws.root
            (ws.root / "cloned.ts").write_text("export const secret = 1;")
            raise RuntimeError("analysis blew up")
    assert root is not None
    assert not root.exists()


async def test_the_context_manager_passes_the_network_policy_through(
    executor: DevelopmentSecureExecutor,
) -> None:
    async with executor.workspace(WorkspaceSpec(run_id="ctx-net", allow_network=True)) as ws:
        assert ws.allow_network is True
    async with executor.workspace(WorkspaceSpec(run_id="ctx-nonet")) as ws:
        assert ws.allow_network is False
