"""Run `infra/confidential-space/launch.sh` in plan mode and read back its command.

Shared by `test_confidential_space_launch.py`, `test_confidential_space_setup.py`
and `test_workload_image.py`, because three copies of "parse the launch plan"
is how one of them stops matching the script. It also holds the single loader
for `entrypoint.py`, for the same reason.

**The command is read the way bash reads it, not the way a Python splitter
does.** The printed command is executed by `bash -c` with `PATH` set to a
directory containing nothing but a recording `gcloud` stand-in, and the argv the
stand-in receives is what the tests assert on. So a quoting mistake in the
printed command — a value that bash would split, expand or unquote differently
— shows up as a wrong argv rather than passing a text comparison. No real
`gcloud` is reachable from that `PATH`, and `launch.sh` itself is only ever run
without `--apply`.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import tests.fake_gcloud as fake_gcloud
from mcpforge.execution.confidential_space import AttestationRun
from mcpforge.relying_party.runs import FileAttestationRunStore

REPO_ROOT = Path(__file__).resolve().parents[3]
INFRA_DIR = REPO_ROOT / "infra" / "confidential-space"
LAUNCH_SCRIPT = INFRA_DIR / "launch.sh"
ENTRYPOINT = INFRA_DIR / "entrypoint.py"

BEGIN_MARKER = "# >>> launch command"
END_MARKER = "# <<< launch command"

#: Records its argv as one JSON line. Written as `/bin/sh` exec'ing the test
#: interpreter by absolute path, because the repository path contains a space
#: and a `#!` line cannot hold one.
_RECORDER = (
    "import json, os, sys; "
    'open(os.environ["LAUNCH_STUB_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")'
)


def _install_recording_gcloud(directory: Path, *, exit_code: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / "gcloud"
    stub.write_text(
        f'#!/bin/sh\n"{sys.executable}" -c \'{_RECORDER}\' "$@"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _read_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


@dataclass(frozen=True)
class LaunchPlan:
    exit_code: int
    stdout: str
    stderr: str
    #: Every `gcloud` invocation `launch.sh` made while planning. Must be empty.
    gcloud_calls: list[list[str]]
    #: The command block between the markers, exactly as printed.
    command_text: str
    #: That command as bash parses it, minus the leading `gcloud`.
    argv: list[str]
    #: The run the relying party issued for this plan, if one was.
    run: AttestationRun | None = None

    @property
    def positional(self) -> list[str]:
        return fake_gcloud.parse_invocation(self.argv)[0]

    @property
    def flags(self) -> dict[str, str]:
        return fake_gcloud.parse_invocation(self.argv)[1]

    @property
    def metadata(self) -> dict[str, str]:
        """`--metadata`, split on the delimiter its own `^~^` prefix declares."""
        raw = self.flags["--metadata"]
        assert raw.startswith("^~^"), f"--metadata does not declare the ~ delimiter: {raw!r}"
        entries: dict[str, str] = {}
        for item in raw[len("^~^") :].split("~"):
            key, separator, value = item.partition("=")
            assert separator, f"metadata entry without a value: {item!r}"
            assert key not in entries, f"metadata key {key} appears twice"
            entries[key] = value
        return entries


def run_store(workspace: Path) -> FileAttestationRunStore:
    """The relying party's run store for one test workspace."""
    return FileAttestationRunStore(workspace / "runs")


def run_launch_plan(workspace: Path, *arguments: str, issue: bool = True) -> LaunchPlan:
    """Run `launch.sh` (never with `--apply`) and parse what it would run.

    With `issue`, the relying party first issues a run in this workspace's store
    — the real `FileAttestationRunStore` — and `--run-id` names it; `launch.sh`
    then reads the audience back through `relying_party show`, as it must.
    """

    if "--apply" in arguments:
        raise AssertionError("the test suite never runs launch.sh --apply")
    run: AttestationRun | None = None
    if issue:
        run = run_store(workspace).issue()
        arguments = ("--run-id", run.run_id, *arguments)

    plan_bin = workspace / "plan-bin"
    plan_log = workspace / "plan-calls.log"
    # Exits non-zero, so a plan that did call gcloud would also fail loudly.
    _install_recording_gcloud(plan_bin, exit_code=97)
    environment = dict(os.environ)
    environment["PATH"] = f"{plan_bin}{os.pathsep}{environment.get('PATH', '')}"
    environment["LAUNCH_STUB_LOG"] = str(plan_log)
    environment["CONFIDENTIAL_SPACE_RUN_DIR"] = str(workspace / "runs")
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", str(LAUNCH_SCRIPT), *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        env=environment,
        cwd=workspace,
        timeout=60,
        check=False,
    )
    calls = _read_calls(plan_log)

    command_text = ""
    argv: list[str] = []
    lines = completed.stdout.splitlines()
    if completed.returncode == 0 and BEGIN_MARKER in lines and END_MARKER in lines:
        command_text = "\n".join(lines[lines.index(BEGIN_MARKER) + 1 : lines.index(END_MARKER)])
        argv = _argv_as_bash_reads_it(command_text, workspace)

    return LaunchPlan(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        gcloud_calls=calls,
        command_text=command_text,
        argv=argv,
        run=run,
    )


def _argv_as_bash_reads_it(command_text: str, workspace: Path) -> list[str]:
    parse_bin = workspace / "parse-bin"
    parse_log = workspace / "parse-calls.log"
    parse_log.unlink(missing_ok=True)
    _install_recording_gcloud(parse_bin, exit_code=0)
    bash = shutil.which("bash")
    assert bash is not None, "bash is required"
    # PATH holds the stand-in and nothing else: the real gcloud cannot be found.
    completed = subprocess.run(  # noqa: S603 - fixed argv; the command is our own plan
        [bash, "--noprofile", "--norc", "-c", command_text],
        capture_output=True,
        text=True,
        env={"PATH": str(parse_bin), "LAUNCH_STUB_LOG": str(parse_log)},
        cwd=workspace,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = _read_calls(parse_log)
    assert len(calls) == 1, f"the printed command should call gcloud exactly once: {calls}"
    return calls[0]


@functools.cache
def load_entrypoint() -> ModuleType:
    """`infra/confidential-space/entrypoint.py`, loaded by file. The one loader."""

    spec = importlib.util.spec_from_file_location("mcpforge_workload_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None, f"cannot load {ENTRYPOINT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
