"""The dependency-install step — F9-01, owner decision 2026-09-11.

Validation runs with no network (`03_SECURITY_ACCESS.md` §3), so a connected
repository's tool checks cannot run unless something gives it a dependency
tree first. This module is that something, and it is the **second** networked
operation MCPForge performs, after the clone. Every constraint below is a
refusal, and each names the test that fails if it stops holding:

- **Its own workspace, with the network.** `npm ci` runs in a workspace created
  with `allow_network=True`, holding nothing but the repository's
  `package.json` and `package-lock.json`. The resulting `node_modules` is copied
  into the validation workspace, which never has the network
  (`test_the_install_runs_networked_and_validation_does_not`).
- **A lockfile is required, and only `npm ci` is run.** `npm ci` installs
  exactly what `package-lock.json` pins and fails rather than resolving. Without
  a lockfile the install is refused with a named reason; there is no fallback
  to `npm install` (`test_an_install_without_a_lockfile_is_refused`).
- **No package runs code.** `--ignore-scripts` is on the argument array, and
  `NPM_CONFIG_IGNORE_SCRIPTS=true` in its environment
  (`test_the_install_command_disables_scripts_and_pins_the_registry`).
- **One registry.** `--registry=https://registry.npmjs.org/` is on the argument
  array, and the lockfile is parsed as JSON before anything runs: every
  package's `resolved` URL must be `https://registry.npmjs.org/…` with an
  integrity hash, or the install is refused
  (`test_a_lockfile_resolving_anywhere_else_is_refused`). A repository `.npmrc`
  is never copied into the install workspace, so it cannot override either.
- **Labelled.** The result is an `InstallRecord` — status, reason, the
  lockfile's SHA-256, its package count, the exact argument array and exit code
  — stored in the validation report, so the report shows where the dependency
  tree came from (`test_the_install_evidence_is_recorded`).

The repository's own `node_modules`, if it ships one, is never used: the
pipeline drops it when copying the clone across, and dependencies come from
this step or not at all.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio.to_thread

from mcpforge.execution.provider import (
    Command,
    SandboxError,
    SecureExecutionProvider,
    Workspace,
    WorkspaceSpec,
    resolve_inside,
)
from mcpforge.logging import get_logger

log = get_logger(__name__)

#: The only registry an install may use.
NPM_REGISTRY = "https://registry.npmjs.org/"
ALLOWED_REGISTRY_HOST = "registry.npmjs.org"

LOCKFILE = "package-lock.json"
MANIFEST = "package.json"

#: A lockfile larger than this is refused rather than parsed.
MAX_LOCKFILE_BYTES = 50 * 1024 * 1024

INSTALL_TIMEOUT_SECONDS = 600

#: Where the manifest and lockfile go inside the install workspace.
INSTALL_DIR = "install"


class InstallStatus(StrEnum):
    INSTALLED = "INSTALLED"
    #: Refused before anything ran: no lockfile, or one MCPForge will not trust.
    REFUSED = "REFUSED"
    #: Ran and did not produce a dependency tree.
    FAILED = "FAILED"


class LockfileRefusedError(Exception):
    """The lockfile is absent or pins something outside the allowed registry."""


@dataclass(frozen=True)
class LockfileFacts:
    sha256: str
    package_count: int


@dataclass(frozen=True)
class InstallRecord:
    """What the install step did. Stored in the validation report, verbatim."""

    status: InstallStatus
    reason: str
    lockfile_sha256: str | None
    package_count: int | None
    argv: tuple[str, ...]
    exit_code: int | None
    registry: str = NPM_REGISTRY

    @property
    def installed(self) -> bool:
        return self.status is InstallStatus.INSTALLED

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "lockfile_sha256": self.lockfile_sha256,
            "package_count": self.package_count,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "registry": self.registry,
        }


def install_command(cwd: str) -> Command:
    """The one install command. `npm ci`, scripts off, registry pinned."""
    return Command(
        argv=(
            "npm",
            "ci",
            "--ignore-scripts",
            f"--registry={NPM_REGISTRY}",
            "--no-audit",
            "--no-fund",
        ),
        cwd=cwd,
        timeout_seconds=INSTALL_TIMEOUT_SECONDS,
        env={
            "NPM_CONFIG_REGISTRY": NPM_REGISTRY,
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NO_COLOR": "1",
        },
    )


def _entries(lock: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every package the lockfile pins, for lockfile versions 1, 2 and 3."""
    packages = lock.get("packages")
    if isinstance(packages, dict):
        for name, entry in packages.items():
            if name == "":
                continue  # the project itself
            if not isinstance(entry, dict):
                raise LockfileRefusedError(f"lockfile entry {name!r} is not an object")
            yield name, entry
        return

    def walk(dependencies: object) -> Iterator[tuple[str, dict[str, Any]]]:
        if not isinstance(dependencies, dict):
            return
        for name, entry in dependencies.items():
            if not isinstance(entry, dict):
                raise LockfileRefusedError(f"lockfile entry {name!r} is not an object")
            yield name, entry
            yield from walk(entry.get("dependencies"))

    yield from walk(lock.get("dependencies"))


def inspect_lockfile(repository: Path) -> LockfileFacts:
    """Refuse a lockfile that is missing, unreadable, or not npm-registry-only.

    Parsed as JSON and checked entry by entry — never searched as text, because
    a text search is satisfied by the right host appearing anywhere at all.
    """
    path = repository / LOCKFILE
    if path.is_symlink() or not path.is_file():
        raise LockfileRefusedError(
            f"The repository has no {LOCKFILE}. MCPForge installs only what a lockfile "
            "pins, with `npm ci`, and never resolves versions itself."
        )
    if path.stat().st_size > MAX_LOCKFILE_BYTES:
        raise LockfileRefusedError(f"{LOCKFILE} is larger than {MAX_LOCKFILE_BYTES} bytes.")
    raw = path.read_bytes()
    try:
        lock = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LockfileRefusedError(f"{LOCKFILE} is not valid JSON: {exc}") from exc
    if not isinstance(lock, dict) or not isinstance(lock.get("lockfileVersion"), int):
        raise LockfileRefusedError(f"{LOCKFILE} declares no lockfileVersion.")

    count = 0
    for name, entry in _entries(lock):
        count += 1
        if entry.get("link") is True or entry.get("inBundle") is True:
            # A workspace link or a bundled dependency: nothing is fetched for it.
            continue
        resolved = entry.get("resolved")
        if not isinstance(resolved, str) or not resolved:
            raise LockfileRefusedError(
                f"{name} has no resolved URL in {LOCKFILE}, so where it would come from "
                "cannot be checked."
            )
        parts = urlsplit(resolved)
        if (
            parts.scheme != "https"
            or parts.hostname != ALLOWED_REGISTRY_HOST
            or parts.port is not None
            or parts.username is not None
        ):
            where = parts.hostname or parts.scheme or resolved[:60]
            raise LockfileRefusedError(
                f"{name} resolves to {where!r}; MCPForge installs only from {NPM_REGISTRY}."
            )
        if not isinstance(entry.get("integrity"), str) or not entry["integrity"]:
            raise LockfileRefusedError(f"{name} carries no integrity hash in {LOCKFILE}.")

    return LockfileFacts(sha256=hashlib.sha256(raw).hexdigest(), package_count=count)


async def install_dependencies(
    executor: SecureExecutionProvider, destination: Workspace, *, app_dir: str
) -> InstallRecord:
    """Install the repository at `<destination>/<app_dir>`'s pinned dependencies.

    Never raises for a refusal or a failed install: both are returned as a
    labelled record, because the caller must store what happened either way.
    """
    repository = resolve_inside(destination, app_dir)
    command = install_command(INSTALL_DIR)
    try:
        facts = inspect_lockfile(repository)
    except LockfileRefusedError as exc:
        return InstallRecord(InstallStatus.REFUSED, str(exc), None, None, (), None)
    manifest = repository / MANIFEST
    if manifest.is_symlink():
        # Checked before `is_file()`, because `is_file()` follows a link: a
        # `package.json` pointing at a host file such as `.env` passed it and
        # was copied into the *networked* install workspace (F9-01 review,
        # round 2). The lockfile already had this refusal; the manifest now has
        # the same one. Any link is refused outright — escaping or not — and
        # the reason never includes the target's contents.
        return InstallRecord(
            InstallStatus.REFUSED,
            f"The repository's {MANIFEST} is a link, not a plain file; MCPForge "
            "never follows a link out of a repository.",
            facts.sha256,
            facts.package_count,
            (),
            None,
        )
    if not manifest.is_file():
        return InstallRecord(
            InstallStatus.REFUSED,
            f"The repository has no {MANIFEST}.",
            facts.sha256,
            facts.package_count,
            (),
            None,
        )

    def failed(reason: str, exit_code: int | None = None) -> InstallRecord:
        return InstallRecord(
            InstallStatus.FAILED,
            reason,
            facts.sha256,
            facts.package_count,
            command.argv,
            exit_code,
        )

    try:
        workspace = await executor.create_workspace(
            WorkspaceSpec(run_id=f"{destination.id}-install", allow_network=True)
        )
    except (SandboxError, OSError) as exc:
        return failed(f"The install workspace could not be created: {exc}")

    try:
        target = resolve_inside(workspace, INSTALL_DIR)

        def stage() -> None:
            # The manifest and the lockfile, and nothing else — not `.npmrc`,
            # not the source, not a shipped `node_modules`.
            target.mkdir()
            shutil.copyfile(repository / MANIFEST, target / MANIFEST)
            shutil.copyfile(repository / LOCKFILE, target / LOCKFILE)

        await anyio.to_thread.run_sync(stage)
        result = await executor.run(workspace, command)
        if not result.ok:
            tail = (result.stderr or result.stdout)[-300:]
            return failed(
                f"`npm ci` exited {result.exit_code}"
                + (" (timed out)" if result.timed_out else "")
                + f": {tail}",
                result.exit_code,
            )
        built = resolve_inside(workspace, f"{INSTALL_DIR}/node_modules")
        if not built.is_dir():
            return failed("`npm ci` exited 0 but produced no node_modules.", result.exit_code)

        destination_tree = resolve_inside(destination, f"{app_dir}/node_modules")

        def copy() -> None:
            shutil.copytree(built, destination_tree, symlinks=True)

        await anyio.to_thread.run_sync(copy)
        log.info(
            "dependencies.installed",
            lockfile_sha256=facts.sha256,
            packages=facts.package_count,
        )
        return InstallRecord(
            InstallStatus.INSTALLED,
            f"Installed {facts.package_count} package(s) from {NPM_REGISTRY} with `npm ci`.",
            facts.sha256,
            facts.package_count,
            command.argv,
            result.exit_code,
        )
    except (SandboxError, OSError) as exc:
        return failed(f"The install could not run: {exc}")
    finally:
        await executor.destroy(workspace)
