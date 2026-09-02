"""Development secure executor.

Real isolation, honestly labelled:

- ephemeral workspace, destroyed on success and on failure
- path jail with symlinks resolved and escapes rejected
- executable allowlist; argument arrays only, never a shell string
- no network for analysis commands
- CPU, memory, wall-clock and output-size limits

It is **not** hardware-backed, `attestation()` returns `None`, and its trust
level is `DEVELOPMENT_ISOLATION`. Nothing here can produce `HARDWARE_ATTESTED`.
"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import anyio.to_thread

from mcpforge.execution.provider import (
    AttestationEvidence,
    Command,
    CommandNotAllowedError,
    CommandResult,
    PathEscapeError,
    SandboxError,
    TrustLevel,
    Workspace,
    WorkspaceSpec,
)
from mcpforge.logging import get_logger

log = get_logger(__name__)

#: Only these executables may run. Anything else is refused before a process
#: is created — no arbitrary shell, ever (03_SECURITY_ACCESS.md §3).
ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {"git", "node", "npm", "npx", "tsc", "eslint", "vitest", "next", "python3", "uv"}
)

MAX_OUTPUT_BYTES = 1_000_000

# Unprivileged namespace flags. Unsharing the user namespace first is what makes
# unsharing the network namespace possible without root.
CLONE_NEWUSER = 0x1000_0000
CLONE_NEWNET = 0x4000_0000


def _network_isolation_available() -> bool:
    """Probe once whether this machine grants unprivileged network namespaces.

    Some kernels disable unprivileged user namespaces. We must know which case
    we are in, because claiming an isolation we do not have is worse than not
    having it (03_SECURITY_ACCESS.md §2).
    """

    def drop_network() -> None:  # pragma: no cover - runs in the child
        os.unshare(CLONE_NEWUSER | CLONE_NEWNET)

    try:
        probe = subprocess.run(
            [sys.executable, "-c", "pass"],
            preexec_fn=drop_network,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


class DevelopmentSecureExecutor:
    """Process isolation on the local machine. Development only."""

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        memory_mb: int = 2048,
        cpu_seconds: int = 600,
        allowed_executables: frozenset[str] = ALLOWED_EXECUTABLES,
        require_network_isolation: bool = True,
    ) -> None:
        self._root = workspace_root or Path(tempfile.gettempdir()) / "mcpforge-workspaces"
        self._memory_mb = memory_mb
        self._cpu_seconds = cpu_seconds
        self._allowed = allowed_executables
        self._require_network_isolation = require_network_isolation
        self._network_isolation = _network_isolation_available()

    @property
    def network_isolation_available(self) -> bool:
        """Whether this machine can actually deny a job the network.

        Reported rather than assumed, so the product never claims a control it
        does not have on the machine it is running on.
        """
        return self._network_isolation

    @property
    def trust_level(self) -> TrustLevel:
        # Never anything else. This implementation cannot attest.
        return TrustLevel.DEVELOPMENT_ISOLATION

    def _make_workspace(self, spec: WorkspaceSpec) -> Workspace:
        self._root.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=f"{spec.run_id}-", dir=self._root))
        path.chmod(0o700)
        return Workspace(
            id=path.name,
            root=path.resolve(),
            trust_level=self.trust_level,
            allow_network=spec.allow_network,
        )

    async def create_workspace(self, spec: WorkspaceSpec) -> Workspace:
        workspace = await anyio.to_thread.run_sync(self._make_workspace, spec)
        log.info("sandbox.workspace_created", run_id=spec.run_id, trust=self.trust_level.value)
        return workspace

    def resolve_inside(self, workspace: Workspace, relative: str) -> Path:
        """Resolve a path and prove it stays inside the jail.

        Symlinks are resolved first, so a link pointing out of the workspace is
        rejected rather than followed.
        """
        candidate = (workspace.root / relative).resolve()
        try:
            candidate.relative_to(workspace.root.resolve())
        except ValueError as exc:
            raise PathEscapeError(f"path {relative!r} resolves outside the workspace") from exc
        return candidate

    async def run(self, workspace: Workspace, command: Command) -> CommandResult:
        if not command.argv:
            raise CommandNotAllowedError("empty command")

        deny_network = not workspace.allow_network
        if deny_network and not self._network_isolation and self._require_network_isolation:
            raise SandboxError(
                "This machine cannot create unprivileged network namespaces, so a job "
                "cannot be denied the network. Refusing rather than running it with "
                "network access while claiming otherwise. Pass allow_network=True on the "
                "workspace if the job genuinely needs the network, or construct the "
                "executor with require_network_isolation=False to accept the weaker "
                "boundary knowingly."
            )

        executable = command.argv[0]
        if executable not in self._allowed:
            raise CommandNotAllowedError(
                f"{executable!r} is not on the allowlist; add it deliberately or not at all"
            )

        cwd = self.resolve_inside(workspace, command.cwd or ".")

        # A deliberately minimal environment. Nothing from the parent process
        # leaks in, so no credential can be read by a repository job.
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(workspace.root),
            "TMPDIR": str(workspace.root),
            "CI": "1",
            **command.env,
        }

        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=self._child_setup(deny_network),
            )
        except FileNotFoundError as exc:
            raise CommandNotAllowedError(f"{executable!r} is not installed") from exc

        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(), timeout=command.timeout_seconds
            )
        except TimeoutError:
            timed_out = True
            # Kill the whole process group, not just the direct child. The child
            # calls setsid(), so a grandchild (git's https helper, npm, next)
            # would otherwise survive holding the output pipes open, and the
            # follow-up communicate() would block forever — the wall-clock limit
            # would silently not exist for any job that forks.
            self._kill_group(process)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=5)
            except TimeoutError:  # pragma: no cover - the group is already dead
                stdout_b, stderr_b = b"", b""

        truncated = len(stdout_b) > MAX_OUTPUT_BYTES or len(stderr_b) > MAX_OUTPUT_BYTES
        result = CommandResult(
            argv=tuple(command.argv),
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            stderr=stderr_b[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            duration_seconds=round(time.monotonic() - started, 3),
            timed_out=timed_out,
            output_truncated=truncated,
        )
        log.info(
            "sandbox.command",
            executable=executable,
            exit_code=result.exit_code,
            timed_out=timed_out,
            duration_seconds=result.duration_seconds,
        )
        return result

    @staticmethod
    def _kill_group(process: asyncio.subprocess.Process) -> None:
        """Kill the whole process group, not just the direct child.

        The child calls setsid(), so a grandchild (git's https helper, npm,
        next) would otherwise survive holding the output pipes open and the
        follow-up communicate() would block forever — meaning the wall-clock
        limit silently would not exist for any job that forks.
        """
        if process.returncode is not None:
            return
        try:
            group = os.getpgid(process.pid)
        except ProcessLookupError:  # pragma: no cover - already gone
            return

        # Only safe because the child calls setsid(). If that ever fails, the
        # child shares OUR group and killpg would SIGKILL this process. Checked
        # rather than assumed: a mutation that skipped the pre-exec hook killed
        # the test runner exactly this way.
        if group == os.getpgrp():
            process.kill()
            return

        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # pragma: no cover - already gone
            process.kill()

    def _child_setup(self, deny_network: bool) -> Callable[[], None]:
        """Build the pre-exec hook: limits always, network namespace when denied."""

        def setup() -> None:  # pragma: no cover - runs in the child process
            if deny_network and self._network_isolation:
                os.unshare(CLONE_NEWUSER | CLONE_NEWNET)
            self._apply_limits()

        return setup

    def _apply_limits(self) -> None:  # pragma: no cover - runs in the child process
        """Applied in the child before exec. Hard limits, not advisory."""
        resource.setrlimit(resource.RLIMIT_CPU, (self._cpu_seconds, self._cpu_seconds))
        limit = self._memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        # Deliberately no RLIMIT_NPROC. It is a per-UID limit, not per-process:
        # setting it here would fail against the whole account's process count
        # and break legitimate work (git clone forks a helper) while isolating
        # nothing. Process containment belongs to the Phase 8 executor.
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.setsid()

    async def attestation(self) -> AttestationEvidence | None:
        """No attestation exists here, and none is invented."""
        return None

    @asynccontextmanager
    async def workspace(self, spec: WorkspaceSpec) -> AsyncIterator[Workspace]:
        """Create a workspace and guarantee it is destroyed.

        03_SECURITY_ACCESS.md §3 requires destruction on success *and* on
        failure. Leaving that to callers means the one path that raises is the
        one that leaks a checkout of someone's private source onto the disk.
        """
        ws = await self.create_workspace(spec)
        try:
            yield ws
        finally:
            await self.destroy(ws)

    async def destroy(self, workspace: Workspace) -> None:
        await anyio.to_thread.run_sync(lambda: shutil.rmtree(workspace.root, ignore_errors=True))
        log.info("sandbox.workspace_destroyed", workspace_id=workspace.id)
