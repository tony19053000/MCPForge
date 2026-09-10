"""`infra/confidential-space/launch.sh` — the next VM, from a run the API issued. F8-02.

The run id and audience come from the relying party: `run_launch_plan` issues a
run in a real `FileAttestationRunStore` and passes `--run-id`; `launch.sh` reads
the audience back through `python -m mcpforge.relying_party show`. The script is
run **in plan mode only** — the helper refuses `--apply` — with `gcloud`
shadowed by a recording stand-in, and the command is read back the way bash
parses it (see `tests/launch_plan.py`).

**What none of this demonstrates.** No VM was created. Whether Google accepts
this exact command, whether the launcher accepts a digest-pinned
`tee-image-reference`, and whether it passes these `tee-env-*` values through
are observable only on a real run.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
from pathlib import Path
from types import ModuleType

import pytest

from mcpforge.execution.confidential_space import check_launcher_audience
from tests.launch_plan import (
    INFRA_DIR,
    LAUNCH_SCRIPT,
    LaunchPlan,
    load_entrypoint,
    run_launch_plan,
    run_store,
)
from tests.test_confidential_space_setup import _README_DIGEST, BANNED_IDENTIFIERS, README

DOCKERFILE = INFRA_DIR / "Dockerfile"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The one Dockerfile parser (`F8-02a`), not a second reading of LABEL lines.
dockerfile_scan = _load(INFRA_DIR / "dockerfile_scan.py", "mcpforge_dockerfile_scan_launch")


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> LaunchPlan:
    result = run_launch_plan(tmp_path_factory.mktemp("launch"))
    assert result.exit_code == 0, result.stdout + result.stderr
    return result


def _tee_env(plan: LaunchPlan) -> dict[str, str]:
    return {
        key.removeprefix("tee-env-"): value
        for key, value in plan.metadata.items()
        if key.startswith("tee-env-")
    }


def _allow_env_override_label() -> set[str]:
    labels: dict[str, str] = {}
    for instruction in dockerfile_scan.instructions(DOCKERFILE.read_text(encoding="utf-8")):
        if dockerfile_scan.verb(instruction) != "LABEL":
            continue
        for pair in shlex.split(instruction.split(None, 1)[1]):
            name, _, value = pair.partition("=")
            labels[name] = value
    declared = labels.get("tee.launch_policy.allow_env_override")
    assert declared, "the Dockerfile declares no allow_env_override label — reading the wrong file"
    return set(declared.split(","))


def _readme_digest() -> str:
    found = _README_DIGEST.search(README.read_text(encoding="utf-8"))
    assert found is not None, "README.md records no digest"
    return found.group(1)


# -- the relying party owns the nonce -----------------------------------------


def test_the_plan_carries_exactly_the_run_the_relying_party_issued(plan: LaunchPlan) -> None:
    """Run id and audience are the issued record's, byte for byte."""

    assert plan.run is not None
    assert _tee_env(plan) == {
        "MCPFORGE_RUN_ID": plan.run.run_id,
        "MCPFORGE_ATTESTATION_AUDIENCE": plan.run.audience,
    }


def test_a_run_the_relying_party_did_not_issue_cannot_be_launched(tmp_path: Path) -> None:
    """There is no way to hand `launch.sh` an audience. A run id the API never
    issued is refused before any command is built."""

    result = run_launch_plan(tmp_path, "--run-id", "cs-20260101-000000-abcdef", issue=False)
    assert result.exit_code != 0
    assert "did not issue" in result.stderr
    assert result.gcloud_calls == []
    assert result.argv == []


def test_a_verified_run_cannot_be_launched_again(tmp_path: Path) -> None:
    store = run_store(tmp_path)
    run = store.issue()
    assert store.consume(run.run_id)
    result = run_launch_plan(tmp_path, "--run-id", run.run_id, issue=False)
    assert result.exit_code != 0
    assert result.argv == []


def test_a_run_id_is_required(tmp_path: Path) -> None:
    result = run_launch_plan(tmp_path, issue=False)
    assert result.exit_code != 0
    assert "--run-id is required" in result.stderr
    assert result.gcloud_calls == []


def test_launch_accepts_no_audience_argument(tmp_path: Path) -> None:
    result = run_launch_plan(tmp_path, "--audience", "mcpforge-attestation-" + "0" * 32)
    assert result.exit_code != 0
    assert result.argv == []


# -- nothing runs ----------------------------------------------------------


def test_the_plan_calls_no_gcloud_command_at_all(plan: LaunchPlan) -> None:
    """Plan mode is not "no mutating call" — it is no call. Creating a VM spends money."""

    assert plan.gcloud_calls == []
    assert "Nothing was run" in plan.stdout


def test_the_command_that_would_run_is_the_command_printed() -> None:
    """Static, and bounded to that: the one `gcloud` invocation in the script is
    the array it prints, and it sits after the plan-mode `return`. It cannot be
    exercised behaviourally without `--apply`, which this suite never runs.
    """

    code = [
        line.strip()
        for line in LAUNCH_SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    runs = [index for index, line in enumerate(code) if line == '"${create[@]}"']
    assert len(runs) == 1, f"expected one execution of the create array, found {len(runs)}"
    guard = [
        index for index, line in enumerate(code) if line == 'if [[ "${mode}" != "apply" ]]; then'
    ]
    assert len(guard) == 1 and guard[0] < runs[0], "the execution is not behind the plan guard"
    start = code.index("local -a create=(")
    end = code.index(")", start)
    outside_the_array = code[:start] + code[end + 1 :]
    other = [
        line
        for line in outside_the_array
        if re.match(r"^(gcloud|command gcloud|exec gcloud)\b", line)
    ]
    assert other == [], f"gcloud is invoked outside the printed array: {other}"


# -- the command ----------------------------------------------------------


def test_the_plan_is_one_confidential_vm_create(plan: LaunchPlan) -> None:
    assert plan.run is not None
    assert plan.positional == ["compute", "instances", "create", f"mcpforge-{plan.run.run_id}"]


def test_the_plan_uses_exactly_the_canonical_launch_flags(plan: LaunchPlan) -> None:
    """Every flag and every value, and no flag beyond them."""

    flags = {name: value for name, value in plan.flags.items() if name != "--metadata"}
    assert flags == {
        "--project": "mcpforge-aa5c2",
        "--zone": "us-central1-b",
        "--confidential-compute-type": "SEV",
        "--machine-type": "n2d-standard-2",
        "--maintenance-policy": "TERMINATE",
        "--shielded-secure-boot": "",
        "--image-project": "confidential-space-images",
        "--image-family": "confidential-space",
        "--service-account": "mcpforge-workload@mcpforge-aa5c2.iam.gserviceaccount.com",
        "--scopes": "cloud-platform",
    }


def test_the_metadata_holds_only_the_image_and_the_environment(plan: LaunchPlan) -> None:
    """No `tee-container-log-redirect`, no `tee-cmd`, no mount."""

    keys = set(plan.metadata)
    assert keys == {"tee-image-reference"} | {f"tee-env-{name}" for name in _tee_env(plan)}


def test_the_plan_supplies_exactly_the_required_environment(plan: LaunchPlan) -> None:
    """Derived from the entrypoint's own `REQUIRED_ENVIRONMENT`, not a list here."""

    required = set(load_entrypoint().REQUIRED_ENVIRONMENT)
    assert len(required) >= 2, "REQUIRED_ENVIRONMENT is suspiciously small — reading the wrong file"
    assert set(_tee_env(plan)) == required


def test_every_supplied_name_is_allowed_by_the_launch_policy(plan: LaunchPlan) -> None:
    allowed = _allow_env_override_label()
    assert set(_tee_env(plan)) <= allowed, set(_tee_env(plan)) - allowed


def test_the_override_surface_is_the_two_per_run_values() -> None:
    """The operator can set the run id and the issued audience, and nothing else.
    In particular not the digest the relying party pins, and not the jail root."""

    assert _allow_env_override_label() == {"MCPFORGE_RUN_ID", "MCPFORGE_ATTESTATION_AUDIENCE"}


def test_the_image_is_pinned_by_the_readme_digest(plan: LaunchPlan) -> None:
    assert plan.metadata["tee-image-reference"] == (
        f"us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor/workload@{_readme_digest()}"
    )


def test_the_issued_values_satisfy_the_entrypoint_and_the_launcher(plan: LaunchPlan) -> None:
    """The entrypoint's own `read_configuration` and the client's own audience
    check — the one implementation of each rule — accept the issued run."""

    values = _tee_env(plan)
    assert load_entrypoint().read_configuration(values) == values
    check_launcher_audience(values["MCPFORGE_ATTESTATION_AUDIENCE"])
    for name, value in values.items():
        assert "~" not in value, f"{name} contains the metadata delimiter"
        assert value == value.strip() and " " not in value, f"{name} contains whitespace"


@pytest.mark.parametrize("banned", BANNED_IDENTIFIERS)
def test_no_banned_identifier_appears_in_the_plan(plan: LaunchPlan, banned: str) -> None:
    """The runtime half. The script's own text is swept, with the setup script,
    by `test_no_banned_project_identifier_appears` and
    `test_the_banned_identifiers_are_named_only_where_they_are_forbidden`."""

    assert banned not in plan.stdout
    assert all(banned not in token for token in plan.argv)
