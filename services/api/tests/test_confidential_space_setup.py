"""Confidential Space workload identity — F8-02b.

The thing that can go wrong here is quiet. A workload identity pool with a
permissive attribute condition federates happily, issues credentials, logs
nothing unusual, and attests to **nothing in particular** — the hardware
guarantee is intact and the statement it makes has been widened to
uselessness. A wildcard in one clause is the whole of that failure.

So the tests below are arranged around one rule learned from `F8-02a`, where a
check matching text *near* a property rather than *at* it survived three review
rounds: **assert against the value gcloud actually receives.** `setup.sh` is
executed against a scripted `gcloud` (`fake_gcloud.py`) that records every
invocation, and the condition, the digest, the issuer, the roles and the
principal set are read out of that recorded `argv`. Nothing here greps the
script for a substring.

Four groups:

1. **Plan and idempotency** — the script changes nothing by default, a first
   `--apply` creates what is missing, and a second makes no change at all.
2. **The condition, as gcloud receives it** — it pins the `F8-02a` digest by
   exact equality, and agrees clause for clause with `execution/attestation.py`
   and with `policy.md`.
3. **Tokens that must not satisfy it** — debug, wrong digest, wrong hardware,
   wrong service account, and every constrained claim missing. Evaluated by
   `setup_scan.evaluate`, whose bound is stated in that module and repeated
   below.
4. **Least privilege and hygiene** — no role beyond `policy.md`, no
   service-account key, no credential and no banned project identifier.

**What none of this demonstrates.** No GCP resource has been created. The
script's mutating paths have never run against Google, `fake_gcloud.py` is our
model of gcloud rather than gcloud, and `setup_scan.evaluate` is our evaluator
over the subset of CEL this condition uses rather than Google's CEL. Live
verification is manual and is recorded in `infra/confidential-space/README.md`,
where the record is currently empty.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import tests.fake_gcloud as fake_gcloud
from mcpforge.execution.attestation import (
    CONFIDENTIAL_SPACE_ISSUER,
    DEFAULT_ALLOWED_HARDWARE_MODELS,
    AttestationPolicy,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
INFRA_DIR = REPO_ROOT / "infra" / "confidential-space"
SETUP_SCRIPT = INFRA_DIR / "setup.sh"
POLICY_DOC = INFRA_DIR / "policy.md"
README = INFRA_DIR / "README.md"
SCAN_MODULE = INFRA_DIR / "setup_scan.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: The digest F8-02a produced, read from the README rather than retyped, so that
#: this file cannot pin a digest the build does not produce.
_README_DIGEST = re.compile(r"^digest:\s*(sha256:[0-9a-f]{64})$", re.MULTILINE)

#: Claim paths, as they appear on the left of a clause.
IMAGE_DIGEST_PATH = "assertion.submods.container.image_digest"
HARDWARE_PATH = "assertion.hwmodel"
DEBUG_PATH = "assertion.dbgstat"
SOFTWARE_PATH = "assertion.swname"
SERVICE_ACCOUNT_PATH = "assertion.google_service_accounts"

WORKLOAD_SERVICE_ACCOUNT = "mcpforge-workload@mcpforge-aa5c2.iam.gserviceaccount.com"


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: `infra/confidential-space/setup_scan.py` — the one clause splitter, the one
#: whitespace rule and the one condition evaluator, shared with `setup.sh`,
#: which runs the same file as a script before it plans anything. A second copy
#: here is exactly the drift `dockerfile_scan.py` was created to end.
setup_scan = _load_module(SCAN_MODULE, "mcpforge_setup_scan")


# ---------------------------------------------------------------------------
# Running the script against a scripted gcloud
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    argv: list[str]
    exit_code: int
    mutating: bool
    recognised: bool


@dataclass(frozen=True)
class Run:
    exit_code: int
    stdout: str
    stderr: str
    invocations: list[Invocation]
    state: dict[str, Any]

    @property
    def mutations(self) -> list[Invocation]:
        return [call for call in self.invocations if call.mutating]

    def flag(self, subcommand: str, flag: str) -> str:
        """The value of ``flag`` on the single invocation containing ``subcommand``."""

        matches = [call for call in self.mutations if subcommand in call.argv]
        assert len(matches) == 1, f"expected one {subcommand} invocation, got {len(matches)}"
        _, flags = _parse_argv(matches[0].argv)
        assert flag in flags, f"{subcommand} was invoked without {flag}: {matches[0].argv}"
        return flags[flag]


def _parse_argv(argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Read an invocation the way `fake_gcloud` read it. One parser, not two."""

    return fake_gcloud.parse_invocation(argv)


def run_setup(
    workspace: Path,
    *arguments: str,
    state: dict[str, Any] | None = None,
    infra_dir: Path = INFRA_DIR,
) -> Run:
    """Execute `setup.sh` with `gcloud` replaced by the scripted stand-in."""

    bin_dir = workspace / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "gcloud"
    shim.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{Path(fake_gcloud.__file__).resolve()}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    state_path = workspace / "state.json"
    if state is not None or not state_path.exists():
        state_path.write_text(
            json.dumps(state if state is not None else fake_gcloud.empty_state(), indent=2),
            encoding="utf-8",
        )
    log_path = workspace / "gcloud.log"
    log_path.write_text("", encoding="utf-8")

    environment = dict(os.environ)
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    environment["FAKE_GCLOUD_STATE"] = str(state_path)
    environment["FAKE_GCLOUD_LOG"] = str(log_path)

    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["bash", str(infra_dir / "setup.sh"), *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        env=environment,
        cwd=workspace,
        timeout=120,
    )
    invocations = [
        Invocation(
            argv=list(entry["argv"]),
            exit_code=int(entry["exit"]),
            mutating=bool(entry["mutating"]),
            recognised=bool(entry["recognised"]),
        )
        for entry in (
            json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line
        )
    ]
    return Run(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        invocations=invocations,
        state=json.loads(state_path.read_text(encoding="utf-8")),
    )


def assert_recognised(run: Run) -> None:
    """No invocation fell through `fake_gcloud`'s dispatch.

    `setup.sh` reads a failing `describe` as "the resource is absent", so an
    unrecognised command would look like something that needs creating and
    every idempotency assertion below would pass on nonsense.
    """

    assert run.invocations, "the script issued no gcloud invocation at all"
    unknown = [call.argv for call in run.invocations if not call.recognised]
    assert unknown == [], f"fake_gcloud did not recognise: {unknown}"


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    directory = tmp_path / "run"
    directory.mkdir()
    yield directory


@pytest.fixture(scope="module")
def applied(tmp_path_factory: pytest.TempPathFactory) -> Run:
    """A first `--apply` against an empty project. The source of the runtime argv.

    Every assertion about "what gcloud receives" reads this run, so the values
    under test are the ones the script actually passed, not text found in it.
    """

    directory = tmp_path_factory.mktemp("applied")
    run = run_setup(directory, "--apply", state=fake_gcloud.empty_state())
    assert run.exit_code == 0, run.stdout + run.stderr
    assert_recognised(run)
    return run


@pytest.fixture(scope="module")
def condition(applied: Run) -> str:
    """The attribute condition as the provider actually received it."""

    return applied.flag("create-oidc", "--attribute-condition")


@pytest.fixture(scope="module")
def clauses(condition: str) -> tuple[str, ...]:
    parsed = setup_scan.split_clauses(condition)
    assert len(parsed) >= 4, f"the condition has suspiciously few clauses: {parsed}"
    return tuple(parsed)


def clause_for(clauses: tuple[str, ...], path: str) -> tuple[str, str, str]:
    """The single clause constraining ``path``, decomposed. Absence is a failure."""

    # `setup_scan` is loaded by file rather than imported, so mypy sees its
    # return as Any; the annotation here is the one that is checked.
    decomposed: list[tuple[str, str, str]] = [
        setup_scan.clause_operands(clause) for clause in clauses
    ]
    found = [parts for parts in decomposed if path in (parts[0], parts[2])]
    assert len(found) == 1, f"expected exactly one clause constraining {path}, got {found}"
    return found[0]


# ---------------------------------------------------------------------------
# 1. Plan and idempotency
# ---------------------------------------------------------------------------


def test_the_default_mode_issues_no_mutating_call(workspace: Path) -> None:
    """No arguments means plan only. This is the guard on "create nothing"."""

    run = run_setup(workspace, state=fake_gcloud.empty_state())
    assert run.exit_code == 0, run.stdout + run.stderr
    assert_recognised(run)
    assert run.mutations == [], f"plan mode invoked a mutating command: {run.mutations}"
    assert run.state == fake_gcloud.empty_state()
    assert "would:" in run.stdout
    assert "Nothing was changed." in run.stdout


def test_the_plan_prints_every_command_it_would_run(workspace: Path) -> None:
    """ "Print what it would change before changing it", asserted as printed text."""

    run = run_setup(workspace, state=fake_gcloud.empty_state())
    printed = [line.strip() for line in run.stdout.splitlines() if line.strip().startswith("$ ")]
    assert len(printed) >= 6, f"the plan printed too few commands: {printed}"
    for expected in (
        "services enable",
        "workload-identity-pools create",
        "providers create-oidc",
        "service-accounts create",
        "add-iam-policy-binding",
    ):
        assert any(expected in line for line in printed), f"the plan never mentions {expected}"


def test_a_first_apply_creates_the_declared_resources(applied: Run) -> None:
    assert applied.mutations, "--apply against an empty project changed nothing"
    state = applied.state
    assert state["pool"] is not None
    assert state["provider"] is not None
    assert state["service_account"] is True
    assert sorted(state["enabled_services"]) == [
        "iam.googleapis.com",
        "iamcredentials.googleapis.com",
        "sts.googleapis.com",
    ]


def test_a_second_apply_changes_nothing(workspace: Path) -> None:
    """The acceptance criterion: run twice, diff the resulting IAM policy."""

    first = run_setup(workspace, "--apply", state=fake_gcloud.empty_state())
    assert first.exit_code == 0, first.stdout + first.stderr
    assert first.mutations, "the first apply changed nothing, so the second proves nothing"
    after_first = json.loads(json.dumps(first.state))

    second = run_setup(workspace, "--apply")  # same state file, carried over
    assert second.exit_code == 0, second.stdout + second.stderr
    assert_recognised(second)
    assert second.mutations == [], f"the second apply mutated: {second.mutations}"
    assert "No changes required." in second.stdout
    assert second.state == after_first, "the IAM policy differs after a second run"
    assert second.state["project_bindings"] == after_first["project_bindings"]
    assert second.state["service_account_bindings"] == after_first["service_account_bindings"]


def test_apply_prints_each_command_before_running_it(applied: Run) -> None:
    """Every executed mutation appears in the output, quoted, as a printed command."""

    printed = "\n".join(
        line for line in applied.stdout.splitlines() if line.strip().startswith("$ ")
    )
    for call in applied.mutations:
        for token in call.argv:
            # `change()` prints with `printf '%q '`, which escapes with
            # backslashes where `shlex.quote` would use single quotes. Compare
            # *parsed tokens* rather than either quoting style: asserting the
            # raw substring fails on any argument containing a space, which is
            # a fact about the test rather than about the script.
            printed_tokens = {
                argument
                for line in printed.splitlines()
                if line.strip().startswith("$ ")
                for argument in shlex.split(line.strip()[2:])
            }
            assert token in printed_tokens, f"{token!r} was executed but never printed"


def test_a_drifted_attribute_condition_is_corrected(workspace: Path) -> None:
    """A provider whose condition has been widened by hand is planned back."""

    state = fake_gcloud.empty_state()
    state["pool"] = {"id": "mcpforge-confidential-space", "state": "ACTIVE"}
    state["provider"] = {
        "id": "mcpforge-attestation",
        "state": "ACTIVE",
        "attribute_condition": "true",
        "attribute_mapping": "google.subject=assertion.sub",
        "issuer_uri": CONFIDENTIAL_SPACE_ISSUER,
    }
    run = run_setup(workspace, state=state)
    assert run.exit_code == 0, run.stdout + run.stderr
    assert_recognised(run)
    assert "drift: attribute condition" in run.stdout
    assert "update-oidc" in run.stdout


def test_verify_is_read_only(workspace: Path) -> None:
    first = run_setup(workspace, "--apply", state=fake_gcloud.empty_state())
    assert first.exit_code == 0, first.stdout + first.stderr
    run = run_setup(workspace, "--verify")
    assert run.exit_code == 0, run.stdout + run.stderr
    assert_recognised(run)
    assert run.mutations == []
    assert "no role beyond policy.md" in run.stdout


def test_verify_reports_a_role_beyond_policy_md(workspace: Path) -> None:
    """Least privilege is a claim about what is absent, so it is checked as one."""

    first = run_setup(workspace, "--apply", state=fake_gcloud.empty_state())
    assert first.exit_code == 0, first.stdout + first.stderr
    state = first.state
    state["project_bindings"].append(["roles/editor", f"serviceAccount:{WORKLOAD_SERVICE_ACCOUNT}"])
    run = run_setup(workspace, "--verify", state=state)
    assert run.exit_code != 0, "an extra role was accepted"
    assert "roles/editor" in run.stdout
    assert "not enumerated in policy.md" in run.stdout
    assert run.mutations == []


def test_verify_reports_a_widened_condition(workspace: Path) -> None:
    first = run_setup(workspace, "--apply", state=fake_gcloud.empty_state())
    state = first.state
    state["provider"]["attribute_condition"] = "true"
    run = run_setup(workspace, "--verify", state=state)
    assert run.exit_code != 0, "a widened live condition was accepted"
    assert "is not the declared one" in run.stdout


def test_the_script_refuses_to_run_when_policy_md_disagrees(
    workspace: Path, tmp_path: Path
) -> None:
    """The self-check is load-bearing: with a clause removed, nothing runs at all."""

    infra = tmp_path / "infra"
    shutil.copytree(INFRA_DIR, infra)
    text = infra.joinpath("policy.md").read_text(encoding="utf-8")
    infra.joinpath("policy.md").write_text(
        text.replace("assertion.dbgstat == 'disabled-since-boot'\n", ""), encoding="utf-8"
    )
    run = run_setup(workspace, "--apply", state=fake_gcloud.empty_state(), infra_dir=infra)
    assert run.exit_code != 0, "the script ran with policy.md and setup.sh disagreeing"
    assert "disagree" in run.stdout + run.stderr
    assert run.mutations == [], "a mutation ran despite the self-check failing"


# ---------------------------------------------------------------------------
# 2. The condition, as gcloud receives it
# ---------------------------------------------------------------------------


def test_the_digest_clause_is_exact_equality_against_the_pinned_digest(
    clauses: tuple[str, ...],
) -> None:
    """The single most important assertion in this file.

    Read from the clause's own operands, not from the presence of the digest
    somewhere in the condition — a `matches` or a `!=` clause would contain the
    digest text too.
    """

    digest = _README_DIGEST.search(README.read_text(encoding="utf-8"))
    assert digest is not None, "the README records no digest"
    left, operator, right = clause_for(clauses, IMAGE_DIGEST_PATH)
    assert left == IMAGE_DIGEST_PATH
    assert operator == "==", f"the digest is not pinned by equality but by {operator!r}"
    assert right == f"'{digest.group(1)}'"


@pytest.mark.parametrize("wildcard", ["*", "?", "%", ".*"])
def test_no_clause_contains_a_wildcard(clauses: tuple[str, ...], wildcard: str) -> None:
    for clause in clauses:
        assert wildcard not in clause, f"clause {clause!r} contains {wildcard!r}"


def test_every_clause_uses_an_operator_this_repository_can_evaluate(
    clauses: tuple[str, ...],
) -> None:
    """A clause nobody can evaluate is a clause whose effect is unknown."""

    for clause in clauses:
        _, operator, _ = setup_scan.clause_operands(clause)
        assert operator in ("==", "in")


def test_the_provider_issuer_is_the_verifiers_issuer(applied: Run) -> None:
    assert applied.flag("create-oidc", "--issuer-uri") == CONFIDENTIAL_SPACE_ISSUER


def test_the_condition_allows_exactly_the_hardware_models_the_verifier_allows(
    clauses: tuple[str, ...],
) -> None:
    left, operator, right = clause_for(clauses, HARDWARE_PATH)
    assert (left, operator) == (HARDWARE_PATH, "in")
    models = {item.strip().strip("'") for item in right.strip("[]").split(",")}
    assert models == set(DEFAULT_ALLOWED_HARDWARE_MODELS)


def test_the_condition_debug_status_is_the_verifiers_debug_status(
    clauses: tuple[str, ...], policy: AttestationPolicy
) -> None:
    left, operator, right = clause_for(clauses, DEBUG_PATH)
    assert (left, operator) == (DEBUG_PATH, "==")
    assert right == f"'{policy.required_debug_status}'"


def test_the_condition_software_name_is_the_verifiers_software_name(
    clauses: tuple[str, ...], policy: AttestationPolicy
) -> None:
    left, operator, right = clause_for(clauses, SOFTWARE_PATH)
    assert (left, operator) == (SOFTWARE_PATH, "==")
    assert right == f"'{policy.required_software_name}'"


def test_the_condition_service_account_is_the_workload_service_account(
    clauses: tuple[str, ...],
) -> None:
    """Membership in the array, not equality against a string.

    The claim is `google_service_accounts` — plural, an array of strings, per
    Google's token-claims reference. An earlier version of this test asserted
    equality against a singular `google_service_account`, a claim no real token
    carries, and passed because the fixtures modelled the same wrong shape. The
    operand order is therefore literal-then-path, which is also the form
    Google's own worked example uses.
    """

    left, operator, right = clause_for(clauses, SERVICE_ACCOUNT_PATH)
    assert (operator, right) == ("in", SERVICE_ACCOUNT_PATH)
    assert left == f"'{WORKLOAD_SERVICE_ACCOUNT}'"


def test_the_principal_set_pins_the_same_digest(applied: Run) -> None:
    """The binding narrows the pool to the attested digest, not to the whole pool."""

    digest = _README_DIGEST.search(README.read_text(encoding="utf-8"))
    assert digest is not None
    # Selected by subcommand, not by flag name. The project-role grants are also
    # `add-iam-policy-binding`; matching on the flag alone picked up all of them
    # and the count assertion failed for a reason that had nothing to do with
    # the property under test.
    bindings = [call for call in applied.mutations if "add-iam-policy-binding" in call.argv]
    on_account = [call for call in bindings if "service-accounts" in call.argv]
    assert len(on_account) == 1, f"expected one service-account binding, got {len(on_account)}"
    _, flags = _parse_argv(on_account[0].argv)
    member = flags["--member"]
    assert member.startswith("principalSet://iam.googleapis.com/projects/")
    assert member.endswith(f"/attribute.image_digest/{digest.group(1)}")
    assert "*" not in member, f"the principal set is not narrowed to a digest: {member}"


def test_the_binding_role_is_workload_identity_user_only(applied: Run) -> None:
    bindings = [call for call in applied.mutations if "add-iam-policy-binding" in call.argv]
    on_account = [call for call in bindings if "service-accounts" in call.argv]
    assert len(on_account) == 1
    _, flags = _parse_argv(on_account[0].argv)
    assert flags["--role"] == "roles/iam.workloadIdentityUser"


def test_every_clause_in_the_script_is_documented_in_policy_md(
    clauses: tuple[str, ...],
) -> None:
    documented = setup_scan.documented_conditions(POLICY_DOC.read_text(encoding="utf-8"))
    missing = [clause for clause in clauses if clause not in documented]
    assert missing == [], f"clauses gcloud receives but policy.md does not explain: {missing}"


def test_every_clause_in_policy_md_is_in_the_script(clauses: tuple[str, ...]) -> None:
    documented = setup_scan.documented_conditions(POLICY_DOC.read_text(encoding="utf-8"))
    extra = [clause for clause in documented if clause not in clauses]
    assert extra == [], f"clauses policy.md claims but gcloud never receives: {extra}"


def test_every_clause_is_explained_in_prose_beside_its_claim(
    clauses: tuple[str, ...],
) -> None:
    """`policy.md` records every condition *in prose*, not merely in the code block."""

    prose = POLICY_DOC.read_text(encoding="utf-8")
    body = prose.split("<!-- END ATTRIBUTE CONDITION -->", 1)[1]
    for clause in clauses:
        left, _, right = setup_scan.clause_operands(clause)
        claim = left if left.startswith("assertion.") else right
        assert claim in body, f"no prose in policy.md discusses {claim}"


# ---------------------------------------------------------------------------
# 3. Tokens that must not satisfy the condition
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def policy() -> AttestationPolicy:
    """The verifier's own policy, so the two layers are compared, not restated."""

    digest = _README_DIGEST.search(README.read_text(encoding="utf-8"))
    assert digest is not None
    return AttestationPolicy(
        audience="test-audience-nonce",
        image_digest=digest.group(1),
        workload_service_account=WORKLOAD_SERVICE_ACCOUNT,
    )


@pytest.fixture(scope="module")
def production_token(policy: AttestationPolicy) -> dict[str, Any]:
    return {
        "swname": policy.required_software_name,
        "hwmodel": "GCP_AMD_SEV_SNP",
        "dbgstat": policy.required_debug_status,
        "google_service_accounts": [WORKLOAD_SERVICE_ACCOUNT],
        "submods": {
            "container": {"image_digest": policy.image_digest},
            "confidential_space": {"support_attributes": ["STABLE", "USABLE", "LATEST"]},
        },
    }


def test_a_production_token_satisfies_the_condition(
    condition: str, production_token: dict[str, Any]
) -> None:
    """The self-guard. Without it every rejection test below could pass vacuously."""

    assert setup_scan.evaluate(condition, production_token) is True


@pytest.mark.parametrize(
    ("description", "path", "value"),
    [
        ("a debugger attached since boot", ("dbgstat",), "enabled"),
        ("no debug claim at all", ("dbgstat",), None),
        ("a non-confidential machine", ("hwmodel",), "GCP_SHIELDED_VM"),
        ("a different software stack", ("swname",), "GCE"),
        (
            "another project's service account",
            ("google_service_accounts",),
            ["other@example.com"],
        ),
        (
            "an image nobody reviewed",
            ("submods", "container", "image_digest"),
            "sha256:" + "0" * 64,
        ),
        (
            "the digest with one character changed",
            ("submods", "container", "image_digest"),
            "sha256:31ed4925d78c88080870b5f0846956833a7ad5dd8f9f387997f423c71c5f1eb3",
        ),
        (
            "an uppercase spelling of the pinned digest",
            ("submods", "container", "image_digest"),
            "SHA256:9DFFEBFBDE81D889A4E93B327BCC25E2B65BF5670F1E50408E2BEE803FB3B7D3",
        ),
    ],
)
def test_a_hostile_token_does_not_satisfy_the_condition(
    condition: str,
    production_token: dict[str, Any],
    description: str,
    path: tuple[str, ...],
    value: str | None,
) -> None:
    """One claim changed at a time, so a rejection is attributable to that claim."""

    token = json.loads(json.dumps(production_token))
    target = token
    for segment in path[:-1]:
        target = target[segment]
    if value is None:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    assert setup_scan.evaluate(condition, token) is False, f"admitted a token with {description}"


def test_a_debug_image_token_does_not_satisfy_the_production_condition(
    condition: str, production_token: dict[str, Any]
) -> None:
    """The debug Confidential Space image does not carry `STABLE`."""

    token = json.loads(json.dumps(production_token))
    token["submods"]["confidential_space"]["support_attributes"] = ["LATEST", "USABLE"]
    assert setup_scan.evaluate(condition, token) is False


def test_every_constrained_claim_is_load_bearing(
    condition: str, production_token: dict[str, Any], clauses: tuple[str, ...]
) -> None:
    """Derived from the clauses themselves, so it cannot go stale as they change.

    Removing the claim any clause reads must make the condition fail. A clause
    that can be deleted from the token without effect is a clause constraining
    nothing.
    """

    paths = set()
    for clause in clauses:
        left, _, right = setup_scan.clause_operands(clause)
        for operand in (left, right):
            if operand.startswith("assertion."):
                paths.add(operand)
    assert len(paths) == len(clauses), f"a clause reads no assertion path: {clauses}"

    for path in sorted(paths):
        token = json.loads(json.dumps(production_token))
        segments = path.split(".")[1:]
        target = token
        for segment in segments[:-1]:
            target = target[segment]
        del target[segments[-1]]
        assert setup_scan.evaluate(condition, token) is False, f"{path} constrains nothing"


@pytest.mark.parametrize(
    "widened",
    [
        "true",
        "assertion.swname == 'CONFIDENTIAL_SPACE' || true",
        "assertion.submods.container.image_digest != ''",
        "assertion.submods.container.image_digest.startsWith('sha256:')",
        "assertion.submods.container.image_digest.matches('sha256:.*')",
    ],
)
def test_a_condition_this_repository_cannot_evaluate_is_refused_not_approximated(
    widened: str, production_token: dict[str, Any]
) -> None:
    """A permissive condition must not slip through as "cannot check, so fine"."""

    with pytest.raises(setup_scan.SetupScanError):
        setup_scan.evaluate(widened, production_token)


# ---------------------------------------------------------------------------
# 4. Least privilege and hygiene
# ---------------------------------------------------------------------------


def test_every_role_granted_is_enumerated_in_policy_md(applied: Run) -> None:
    granted = _granted_project_roles(applied)
    documented = setup_scan.documented_roles(POLICY_DOC.read_text(encoding="utf-8"))
    assert granted, "the run granted no project role at all"
    assert sorted(granted) == sorted(documented)


def test_every_role_in_policy_md_is_actually_granted(applied: Run) -> None:
    documented = setup_scan.documented_roles(POLICY_DOC.read_text(encoding="utf-8"))
    missing = [role for role in documented if role not in _granted_project_roles(applied)]
    assert missing == [], f"policy.md enumerates roles the script never grants: {missing}"


def test_every_role_is_justified_in_prose(applied: Run) -> None:
    prose = POLICY_DOC.read_text(encoding="utf-8")
    body = prose.split("<!-- END PROJECT ROLES -->", 1)[1]
    for role in _granted_project_roles(applied):
        assert role in body, f"policy.md grants {role} without saying why"


def _granted_project_roles(run: Run) -> list[str]:
    roles: list[str] = []
    for call in run.mutations:
        positional, flags = _parse_argv(call.argv)
        if positional[:2] == ["projects", "add-iam-policy-binding"]:
            roles.append(flags["--role"])
    return roles


def test_no_basic_or_write_role_is_granted(applied: Run) -> None:
    """Named separately from the policy.md comparison, because these are the ones
    that would matter most if `policy.md` were edited to permit them."""

    forbidden = {"roles/owner", "roles/editor", "roles/viewer"}
    granted = set(_granted_project_roles(applied))
    assert not granted & forbidden
    assert not any(role.endswith(".admin") or role.endswith(".writer") for role in granted)


def test_the_setup_script_never_creates_a_service_account_key(applied: Run) -> None:
    """MCPForge supports no key file anywhere; federation is what replaces one."""

    text = SETUP_SCRIPT.read_text(encoding="utf-8")
    assert "keys create" not in text
    assert "--key-file" not in text
    for call in applied.invocations:
        assert "keys" not in call.argv, f"a key command was issued: {call.argv}"


def test_the_script_carries_no_credential_material() -> None:
    text = SETUP_SCRIPT.read_text(encoding="utf-8") + POLICY_DOC.read_text(encoding="utf-8")
    for pattern in ("AIza", "AQ.", "-----BEGIN", "private_key"):
        assert pattern not in text, f"{pattern!r} appears in the F8-02b infrastructure files"


#: Identifiers that must never name a live MCPForge resource. `STATUS.md` records
#: why: `launchforge-tee` is a real, accessible project in the owner's account
#: rather than a dead placeholder, so a stray reference works silently instead of
#: failing loudly.
BANNED_IDENTIFIERS = (
    "launchforge-tee",
    "launchforge-secure-executor",
    "europe-west4",
    "europe-docker.pkg.dev",
)


@pytest.mark.parametrize("banned", BANNED_IDENTIFIERS)
def test_no_banned_project_identifier_appears(banned: str) -> None:
    """No banned identifier in anything that *acts*.

    Checked against code, not raw text. `setup.sh` and `policy.md` both name
    these strings deliberately, in prose whose entire purpose is to forbid them
    — and a whole-file scan is satisfied by, or in this case broken by, exactly
    that. It is the same shape as `assert "--require-hashes" in dockerfile`
    passing because of the comment that explains the flag: a check must match
    where the property lives. Prose is covered instead by
    `test_the_banned_identifiers_are_named_only_where_they_are_forbidden`.
    """

    for path in (SETUP_SCRIPT, SCAN_MODULE):
        code = _code_only(path)
        assert banned not in code, f"{banned} appears in executable content of {path.name}"


def _code_only(path: Path) -> str:
    """`path` with whole-line comments removed — enough for this check.

    Both files put their banned-identifier notice on its own comment lines, so
    a trailing-comment case does not arise here. If one ever does, the string
    would be flagged, which is the safe direction.
    """

    comment = "#"
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith(comment)
    )


def test_the_banned_identifiers_are_named_only_where_they_are_forbidden() -> None:
    """Prose may name a banned identifier only to ban it.

    The counterweight to `test_no_banned_project_identifier_appears`, which
    ignores comments: without this, moving a real reference into a comment
    would silence that check entirely.

    Read in blocks of consecutive prose lines, not line by line. The notice in
    `setup.sh` wraps, so the identifier lands on one line and the word that
    forbids it on the next — a line-oriented version of this test failed on
    exactly that, which is the same mistake as reading a Dockerfile without
    joining its continuations.
    """

    forbidding = ("never", "not ", "no ", "banned", "forbidden", "wrong", "must")
    for path in (SETUP_SCRIPT, POLICY_DOC, SCAN_MODULE):
        lines = path.read_text(encoding="utf-8").splitlines()
        blocks: list[tuple[int, list[str]]] = []
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            prose = stripped.startswith(("#", ">", "-", "*", "|")) or not stripped
            if prose and blocks and blocks[-1][0] + len(blocks[-1][1]) == number:
                blocks[-1][1].append(stripped)
            elif prose:
                blocks.append((number, [stripped]))
        for number, block in blocks:
            text = " ".join(block).lower()
            for banned in BANNED_IDENTIFIERS:
                if banned not in text:
                    continue
                assert any(word in text for word in forbidding), (
                    f"{path.name}:{number} names {banned} without forbidding it"
                )

        code = _code_only(path)
        for banned in BANNED_IDENTIFIERS:
            assert banned not in code, f"{banned} appears in executable content of {path.name}"


def test_the_env_example_declares_the_new_names_with_no_values() -> None:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    declared = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].split("#", 1)[0].strip()
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    }
    for name in (
        "CONFIDENTIAL_SPACE_WORKLOAD_IDENTITY_POOL",
        "CONFIDENTIAL_SPACE_WORKLOAD_IDENTITY_PROVIDER",
        "CONFIDENTIAL_SPACE_WORKLOAD_SERVICE_ACCOUNT",
    ):
        assert name in declared, f".env.example does not declare {name}"
        assert declared[name] == "", f"{name} carries a value in .env.example"


def test_the_readme_records_the_live_verification_state_honestly() -> None:
    """The record exists, names what would be recorded, and does not claim a run."""

    text = README.read_text(encoding="utf-8")
    assert "Live verification record" in text
    assert "F8-02b" in text
    for claim in ("HARDWARE_ATTESTED", "attestation verified", "TEE VERIFIED"):
        section = text.split("Live verification record", 1)[1]
        assert claim not in section, f"the verification record claims {claim!r}"
