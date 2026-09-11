"""The networked dependency-install step — owner decision 2026-09-11.

Each constraint in `03_SECURITY_ACCESS.md` §3 for the install step has a test
here. The executor below is a test double: real temporary directories, and a
scripted `npm ci` that writes a placeholder tree — **a stand-in for the npm
registry; nothing is downloaded.** What is real is every decision the install
step makes: the lockfile parse, the refusals, the command, the workspaces and
the copy.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from mcpforge.execution.provider import (
    AttestationEvidence,
    Command,
    CommandResult,
    TrustLevel,
    Workspace,
    WorkspaceSpec,
)
from mcpforge.orchestration.dependencies import (
    NPM_REGISTRY,
    InstallStatus,
    LockfileRefusedError,
    inspect_lockfile,
    install_command,
    install_dependencies,
)

APP = "repo"


def _lock(**packages: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "app",
        "lockfileVersion": 3,
        "packages": {"": {"name": "app"}, **packages},
    }


GOOD = _lock(
    **{
        "node_modules/next": {
            "version": "16.3.4",
            "resolved": "https://registry.npmjs.org/next/-/next-16.3.4.tgz",
            "integrity": "sha512-placeholder",
        },
        "node_modules/react": {
            "version": "19.2.8",
            "resolved": "https://registry.npmjs.org/react/-/react-19.2.8.tgz",
            "integrity": "sha512-placeholder",
        },
    }
)


class InstallDouble:
    """TEST ONLY. Real directories; `npm ci` scripted as a registry stand-in."""

    def __init__(self, root: Path, *, exit_code: int = 0) -> None:
        self._root = root
        self.exit_code = exit_code
        self.runs: list[tuple[Command, Workspace, list[str]]] = []

    @property
    def trust_level(self) -> TrustLevel:
        return TrustLevel.DEVELOPMENT_ISOLATION

    async def create_workspace(self, spec: WorkspaceSpec) -> Workspace:
        path = Path(tempfile.mkdtemp(prefix=f"{spec.run_id}-", dir=self._root)).resolve()  # noqa: ASYNC240 - a test double's temporary directory
        return Workspace(
            id=path.name,
            root=path,
            trust_level=self.trust_level,
            allow_network=spec.allow_network,
        )

    async def run(self, workspace: Workspace, command: Command) -> CommandResult:
        cwd = workspace.root / (command.cwd or ".")
        self.runs.append((command, workspace, sorted(p.name for p in cwd.iterdir())))
        if self.exit_code == 0:
            (cwd / "node_modules" / "next").mkdir(parents=True)
            (cwd / "node_modules" / "next" / "index.js").write_text("// stand-in\n")
        return CommandResult(
            argv=command.argv,
            exit_code=self.exit_code,
            stdout="",
            stderr="npm ERR! scripted" if self.exit_code else "",
            duration_seconds=0.0,
        )

    async def attestation(self) -> AttestationEvidence | None:
        return None

    async def destroy(self, workspace: Workspace) -> None:
        shutil.rmtree(workspace.root, ignore_errors=True)


def _destination(tmp_path: Path, lock: dict[str, Any] | None, *, npmrc: bool = False) -> Workspace:
    root = (tmp_path / "validation").resolve()
    app = root / APP
    app.mkdir(parents=True)
    (app / "package.json").write_text('{"name": "app"}')
    if lock is not None:
        (app / "package-lock.json").write_text(json.dumps(lock))
    if npmrc:
        (app / ".npmrc").write_text(
            "registry=https://packages.example.test/\nignore-scripts=false\n"
        )
    return Workspace(
        id="validation",
        root=root,
        trust_level=TrustLevel.DEVELOPMENT_ISOLATION,
        allow_network=False,
    )


def _double(tmp_path: Path, **kwargs: Any) -> InstallDouble:
    root = tmp_path / "workspaces"
    root.mkdir()
    return InstallDouble(root, **kwargs)


# -- the lockfile -------------------------------------------------------------


async def test_an_install_without_a_lockfile_is_refused(tmp_path: Path) -> None:
    executor = _double(tmp_path)
    record = await install_dependencies(executor, _destination(tmp_path, None), app_dir=APP)
    assert record.status is InstallStatus.REFUSED
    assert "no package-lock.json" in record.reason
    assert "never resolves versions" in record.reason
    assert executor.runs == [], "no lockfile, yet something ran"


@pytest.mark.parametrize(
    "resolved",
    [
        "https://packages.example.test/next-16.3.4.tgz",
        "http://registry.npmjs.org/next/-/next-16.3.4.tgz",
        "https://registry.npmjs.org.example.test/next.tgz",
        "https://user@registry.npmjs.org/next.tgz",
        "https://registry.npmjs.org:8443/next.tgz",
        "git+ssh://git@github.com/vercel/next.js.git",
        "file:../next",
    ],
)
async def test_a_lockfile_resolving_anywhere_else_is_refused(tmp_path: Path, resolved: str) -> None:
    lock = json.loads(json.dumps(GOOD))
    lock["packages"]["node_modules/react"]["resolved"] = resolved
    executor = _double(tmp_path)
    record = await install_dependencies(executor, _destination(tmp_path, lock), app_dir=APP)
    assert record.status is InstallStatus.REFUSED
    assert "node_modules/react" in record.reason
    assert executor.runs == []


def test_a_lockfile_v1_is_checked_too(tmp_path: Path) -> None:
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 1,
                "dependencies": {
                    "a": {
                        "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",
                        "integrity": "sha512-x",
                        "dependencies": {
                            "b": {"resolved": "https://evil.example.test/b.tgz", "integrity": "x"}
                        },
                    }
                },
            }
        )
    )
    with pytest.raises(LockfileRefusedError, match=r"evil\.example\.test"):
        inspect_lockfile(tmp_path)


@pytest.mark.parametrize(
    ("entry", "refused"),
    [
        ({"version": "1.0.0", "resolved": "https://registry.npmjs.org/x/-/x-1.0.0.tgz"}, True),
        ({"version": "1.0.0"}, True),
        ({"resolved": "packages/local", "link": True}, False),
        ({"version": "1.0.0", "inBundle": True}, False),
    ],
)
def test_integrity_is_required_and_only_links_and_bundles_are_unresolved(
    tmp_path: Path, entry: dict[str, Any], refused: bool
) -> None:
    (tmp_path / "package-lock.json").write_text(json.dumps(_lock(**{"node_modules/x": entry})))
    if refused:
        with pytest.raises(LockfileRefusedError):
            inspect_lockfile(tmp_path)
    else:
        assert inspect_lockfile(tmp_path).package_count == 1


@pytest.mark.parametrize("text", ["{not json", '["a list"]', '{"packages": {}}'])
def test_an_unreadable_lockfile_is_refused(tmp_path: Path, text: str) -> None:
    (tmp_path / "package-lock.json").write_text(text)
    with pytest.raises(LockfileRefusedError):
        inspect_lockfile(tmp_path)


# -- the command --------------------------------------------------------------


def test_the_install_command_disables_scripts_and_pins_the_registry() -> None:
    """Asserted on the argument array the executor receives, not on file text."""
    command = install_command("install")
    assert command.argv[:2] == ("npm", "ci"), "only `npm ci`, which refuses to resolve"
    assert "install" not in command.argv[1:2]
    assert "--ignore-scripts" in command.argv
    assert f"--registry={NPM_REGISTRY}" in command.argv
    assert NPM_REGISTRY == "https://registry.npmjs.org/"
    assert command.env["NPM_CONFIG_REGISTRY"] == NPM_REGISTRY
    assert command.env["NPM_CONFIG_IGNORE_SCRIPTS"] == "true"


async def test_the_install_runs_the_command_it_records(tmp_path: Path) -> None:
    executor = _double(tmp_path)
    record = await install_dependencies(executor, _destination(tmp_path, GOOD), app_dir=APP)
    assert [c.argv for c, _, _ in executor.runs] == [install_command("install").argv]
    assert record.argv == executor.runs[0][0].argv


# -- the workspaces -----------------------------------------------------------


async def test_the_install_runs_networked_and_validation_does_not(tmp_path: Path) -> None:
    executor = _double(tmp_path)
    destination = _destination(tmp_path, GOOD)
    record = await install_dependencies(executor, destination, app_dir=APP)

    assert record.status is InstallStatus.INSTALLED
    ((_, install_workspace, _),) = executor.runs
    assert install_workspace.allow_network is True
    assert install_workspace.root != destination.root
    assert destination.allow_network is False
    assert (destination.root / APP / "node_modules" / "next" / "index.js").is_file()
    assert not install_workspace.root.exists(), "the install workspace was not destroyed"


async def test_only_the_manifest_and_lockfile_reach_the_install_workspace(tmp_path: Path) -> None:
    """A repository `.npmrc` could re-point the registry or re-enable scripts."""
    executor = _double(tmp_path)
    await install_dependencies(executor, _destination(tmp_path, GOOD, npmrc=True), app_dir=APP)
    ((_, _, staged),) = executor.runs
    assert staged == ["package-lock.json", "package.json"]


async def test_a_symlinked_manifest_is_refused_and_nothing_leaves_the_jail(tmp_path: Path) -> None:
    """A `package.json` that is a link is refused before anything is staged.

    F9-01 review, round 2: `is_file()` follows a link, so a manifest pointing at
    a host file passed the check and was copied into the *networked* install
    workspace — reproduced with a target holding a planted key. The lockfile
    already refused links; this pins the manifest's refusal, and that the
    target's contents reach nothing: not the record, not any workspace.
    """
    secret = "GEMINI_API_KEY=planted-outside-the-jail"
    outside = (tmp_path / "host.env").resolve()
    outside.write_text(secret)
    destination = _destination(tmp_path, GOOD)
    manifest = destination.root / APP / "package.json"
    manifest.unlink()
    manifest.symlink_to(outside)

    executor = _double(tmp_path)
    record = await install_dependencies(executor, destination, app_dir=APP)

    assert record.status is InstallStatus.REFUSED
    assert executor.runs == [], "npm ci ran although the manifest was a link"
    assert secret not in repr(record)
    for path in (tmp_path / "workspaces").rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert secret not in path.read_text(errors="ignore"), (
                f"{path} holds the link target's contents"
            )


# -- the evidence -------------------------------------------------------------


async def test_the_install_evidence_is_recorded(tmp_path: Path) -> None:
    destination = _destination(tmp_path, GOOD)
    expected_sha = hashlib.sha256(
        (destination.root / APP / "package-lock.json").read_bytes()
    ).hexdigest()
    record = await install_dependencies(_double(tmp_path), destination, app_dir=APP)
    payload = record.as_payload()
    assert payload == {
        "status": "INSTALLED",
        "reason": payload["reason"],
        "lockfile_sha256": expected_sha,
        "package_count": 2,
        "argv": list(install_command("install").argv),
        "exit_code": 0,
        "registry": NPM_REGISTRY,
    }
    assert "2 package(s)" in payload["reason"]


async def test_a_failed_install_is_labelled_not_raised(tmp_path: Path) -> None:
    destination = _destination(tmp_path, GOOD)
    record = await install_dependencies(_double(tmp_path, exit_code=1), destination, app_dir=APP)
    assert record.status is InstallStatus.FAILED
    assert record.exit_code == 1
    assert record.lockfile_sha256 is not None
    assert not (destination.root / APP / "node_modules").exists()
