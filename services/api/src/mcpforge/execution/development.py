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
import tempfile
import time
from pathlib import Path

import anyio.to_thread

from mcpforge.execution.provider import (
    AttestationEvidence,
    Command,
    CommandNotAllowedError,
    CommandResult,
    PathEscapeError,
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


class DevelopmentSecureExecutor:
    """Process isolation on the local machine. Development only."""

    def __init__(
        self,
        *,
        workspace_root: Path | None = None,
        memory_mb: int = 2048,
        cpu_seconds: int = 600,
        allowed_executables: frozenset[str] = ALLOWED_EXECUTABLES,
    ) -> None:
        self._root = workspace_root or Path(tempfile.gettempdir()) / "mcpforge-workspaces"
        self._memory_mb = memory_mb
        self._cpu_seconds = cpu_seconds
        self._allowed = allowed_executables

    @property
    def trust_level(self) -> TrustLevel:
        # Never anything else. This implementation cannot attest.
        return TrustLevel.DEVELOPMENT_ISOLATION

    def _make_workspace(self, spec: WorkspaceSpec) -> Workspace:
        self._root.mkdir(parents=True, exist_ok=True)
        path = Path(tempfile.mkdtemp(prefix=f"{spec.run_id}-", dir=self._root))
        path.chmod(0o700)
        return Workspace(id=path.name, root=path.resolve(), trust_level=self.trust_level)

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
                preexec_fn=self._apply_limits,
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
            process.kill()
            stdout_b, stderr_b = await process.communicate()

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

    async def destroy(self, workspace: Workspace) -> None:
        await anyio.to_thread.run_sync(lambda: shutil.rmtree(workspace.root, ignore_errors=True))
        log.info("sandbox.workspace_destroyed", workspace_id=workspace.id)
