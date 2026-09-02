"""Secure execution — 02_ARCHITECTURE.md §8, 03_SECURITY_ACCESS.md §2-§3.

Repository jobs run inside a boundary. Two implementations are planned:

- `DevelopmentSecureExecutor` — real process isolation, real limits, real path
  jail. **Not hardware-backed**, and it says so.
- `ConfidentialSpaceSecureExecutor` — Phase 8, blocked on real GCP
  infrastructure. It is not simulated.

`TrustLevel` is an enum, never a boolean. `HARDWARE_ATTESTED` is assignable only
by code that has verified a real attestation, and no such code exists yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class TrustLevel(StrEnum):
    """What the execution boundary actually is. The UI renders this, not a boolean.

    There is no configuration flag, environment variable or test fixture that can
    produce HARDWARE_ATTESTED without a verified attestation token.
    """

    DEVELOPMENT_ISOLATION = "DEVELOPMENT_ISOLATION"
    HARDWARE_ATTESTED = "HARDWARE_ATTESTED"


@dataclass(frozen=True)
class AttestationEvidence:
    """Only produced by a verified attestation. Phase 8."""

    issuer: str
    image_digest: str
    verified_at: str


@dataclass(frozen=True)
class WorkspaceSpec:
    run_id: str
    #: Analysis and validation commands get no network. Only cloning does.
    allow_network: bool = False


@dataclass(frozen=True)
class Workspace:
    id: str
    root: Path
    trust_level: TrustLevel
    #: False for analysis and validation. Only cloning needs the network.
    allow_network: bool = False


@dataclass(frozen=True)
class Command:
    """An argument array, never a shell string.

    03_SECURITY_ACCESS.md §3: commands are built from an allowlist with argument
    arrays. There is no interpolation into a shell anywhere.
    """

    argv: tuple[str, ...]
    cwd: str | None = None
    timeout_seconds: int = 120
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    output_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxError(Exception):
    """A sandbox rule was violated. Never downgraded to a warning."""


class PathEscapeError(SandboxError):
    """A path resolved outside the workspace."""


class CommandNotAllowedError(SandboxError):
    """The executable is not on the allowlist."""


@runtime_checkable
class SecureExecutionProvider(Protocol):
    @property
    def trust_level(self) -> TrustLevel: ...

    async def create_workspace(self, spec: WorkspaceSpec) -> Workspace: ...

    async def run(self, workspace: Workspace, command: Command) -> CommandResult: ...

    async def attestation(self) -> AttestationEvidence | None: ...

    async def destroy(self, workspace: Workspace) -> None: ...
