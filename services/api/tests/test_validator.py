"""Agent 5 and the Agent Readiness Score — ticket F8-04.

The claim under test is narrow and checkable: **the score is arithmetic over
commands that were executed, and nothing else touches it.** So the tests come in
four groups.

1. *Scoring arithmetic*, including the two cases the ticket names — a component
   with no evidence, and the weight sum.
2. *Structural sweeps* over the backend source, in the shape F8-01 established:
   exactly one producer, a non-empty self-guard so a sweep that scans nothing
   cannot report green, and a stated limit. These are the tests that make "Gemini
   is never asked for a score" a property of the tree rather than of this file.
3. *Behavioural tests against a fake executor*, which is where "the verdict comes
   from exit codes, not model text" is actually demonstrated: the fake returns
   output that says the opposite of its exit code, in both directions.
4. *An integration run against the demo fixture*, which applies the real
   generated patch to the real fixture application and runs the real suite inside
   the real sandbox. It skips locally when the Node toolchain is not present and
   **fails instead of skipping** under
   `MCPFORGE_VALIDATION_INTEGRATION_REQUIRED=1`, which CI sets — a skip that is
   green forever proves nothing.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

from mcpforge.agents.validator import (
    HARNESS_CONFIG,
    HARNESS_DIR,
    NO_INPUTS_SKIP_REASON,
    UI_SYNCHRONIZATION_SKIP_REASON,
    VITEST_CLI,
    ApplicationFacts,
    PlannedCheck,
    ValidationReport,
    Validator,
    ValidatorError,
    alias_specifier,
    harness_files,
    plan_checks,
    vitest_ran_a_test,
)
from mcpforge.execution.development import ALLOWED_EXECUTABLES, DevelopmentSecureExecutor
from mcpforge.execution.provider import (
    Command,
    CommandNotAllowedError,
    CommandResult,
    PathEscapeError,
    SandboxError,
    Workspace,
    WorkspaceSpec,
)
from mcpforge.execution.provider import TrustLevel as _TrustLevel
from mcpforge.generation.nextjs import generate_patch
from mcpforge.generation.test_template import rejection_test_name
from mcpforge.models.analysis import Evidence, RiskClass
from mcpforge.models.webmcp import (
    CallStyle,
    SourceBinding,
    ToolInputProperty,
    WebMCPTool,
    WebMCPToolset,
)
from mcpforge.orchestration import scoring
from mcpforge.orchestration.scoring import (
    COMPONENT_WEIGHTS,
    MAX_EXCERPT_CHARS,
    NO_EVIDENCE_DETAIL,
    TOTAL_POINTS,
    CheckEvidence,
    ExecutedCheck,
    ScoreComponent,
    ScoreTableError,
    score_components,
)
from tests.structure import SRC, call_sites, imported_modules, python_files

REPO_ROOT = Path(__file__).resolve().parents[3]
ARCHITECTURE = REPO_ROOT / "02_ARCHITECTURE.md"
DEMO_APP = REPO_ROOT / "fixtures" / "demo-hotel-app"
NODE_MODULES = REPO_ROOT / "node_modules"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _toolset() -> WebMCPToolset:
    """Two read tools and one gated tool, mapped onto the real demo fixture.

    The same shape `scripts/generate_check.py` uses, so the integration run
    below is generating against functions that genuinely exist.
    """
    return WebMCPToolset(
        tools=[
            WebMCPTool(
                name="search_rooms",
                title="Search rooms",
                description="Find rooms matching a guest count and price ceiling.",
                inputs=[
                    ToolInputProperty(
                        name="guests", json_type="integer", description="Number of guests"
                    ),
                    ToolInputProperty(
                        name="maxPrice",
                        json_type="number",
                        description="Highest nightly price",
                        required=False,
                    ),
                ],
                output_description="Rooms matching the criteria.",
                risk=RiskClass.READ,
                approval_required=False,
                source=SourceBinding(
                    module="@/lib/rooms",
                    symbol="searchRooms",
                    call_style=CallStyle.OBJECT,
                    parameters=["guests", "maxPrice"],
                ),
                evidence=[Evidence(path="src/lib/rooms.ts", symbol="searchRooms")],
            ),
            WebMCPTool(
                name="check_availability",
                title="Check room availability",
                description="Check whether a room is free for a date range.",
                inputs=[
                    ToolInputProperty(name="roomId", json_type="string", description="Room id"),
                    ToolInputProperty(name="checkIn", json_type="string", description="ISO date"),
                    ToolInputProperty(name="checkOut", json_type="string", description="ISO date"),
                ],
                output_description="Availability and total price.",
                risk=RiskClass.READ,
                approval_required=False,
                source=SourceBinding(
                    module="@/lib/availability",
                    symbol="checkAvailability",
                    parameters=["roomId", "checkIn", "checkOut"],
                ),
                evidence=[Evidence(path="src/lib/availability.ts", symbol="checkAvailability")],
            ),
            WebMCPTool(
                name="cancel_reservation",
                title="Cancel a reservation",
                description="Cancel an existing booking.",
                inputs=[
                    ToolInputProperty(
                        name="reservationId", json_type="string", description="Booking id"
                    )
                ],
                output_description="The cancelled reservation.",
                risk=RiskClass.DESTRUCTIVE,
                approval_required=True,
                source=SourceBinding(
                    module="@/lib/reservations",
                    symbol="cancelReservation",
                    parameters=["reservationId"],
                ),
                evidence=[Evidence(path="src/lib/reservations.ts", symbol="cancelReservation")],
            ),
        ]
    )


@pytest.fixture
def toolset() -> WebMCPToolset:
    return _toolset()


@pytest.fixture
def facts() -> ApplicationFacts:
    """An application with everything the suite can use."""
    return ApplicationFacts(
        scripts=frozenset({"test", "typecheck", "build"}), vitest_available=True
    )


def evidence(check_id: str, *, exit_code: int = 0, timed_out: bool = False) -> CheckEvidence:
    return CheckEvidence.from_result(
        check_id,
        CommandResult(
            argv=("node", "x"),
            exit_code=exit_code,
            stdout="",
            stderr="",
            duration_seconds=0.1,
            timed_out=timed_out,
        ),
    )


def executed(
    check_id: str,
    component: ScoreComponent | None,
    *,
    exit_code: int = 0,
    timed_out: bool = False,
) -> ExecutedCheck:
    return ExecutedCheck(
        check_id=check_id,
        component=component,
        description=check_id,
        evidence=evidence(check_id, exit_code=exit_code, timed_out=timed_out),
    )


def one_passing_check_per_component() -> list[ExecutedCheck]:
    return [executed(f"c:{c.value}", c) for c in ScoreComponent]


# ---------------------------------------------------------------------------
# 1. the weights
# ---------------------------------------------------------------------------


def _architecture_weight_table() -> dict[str, int]:
    """Parse §11's table out of the architecture document itself.

    The document is the source. Copying the numbers into this file would make
    the test agree with the code by construction and with nothing else.
    """
    text = ARCHITECTURE.read_text()
    start = text.index("## 11. Agent Readiness Score")
    end = text.index("## 12.", start)
    rows: dict[str, int] = {}
    for line in text[start:end].splitlines():
        match = re.fullmatch(r"\|\s*(.+?)\s*\|\s*(\d+)\s*\|", line.strip())
        if match:
            rows[match.group(1)] = int(match.group(2))
    return rows


def test_the_weights_are_exactly_the_ones_in_the_architecture_document() -> None:
    rows = _architecture_weight_table()
    assert len(rows) == len(ScoreComponent), (
        f"§11's table has {len(rows)} weighted rows and the enum has {len(ScoreComponent)} "
        "members; the parse or the document has drifted"
    )

    from_code = {component.label: COMPONENT_WEIGHTS[component] for component in ScoreComponent}
    assert from_code == rows


def test_the_weights_sum_to_one_hundred() -> None:
    assert sum(COMPONENT_WEIGHTS.values()) == TOTAL_POINTS == 100


def test_a_weight_table_that_does_not_sum_to_one_hundred_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sum check must be able to fail, so break it on purpose.

    `_check_weight_table` runs at import; this drives it directly with a
    tampered table, which is the only way to observe it rejecting one.
    """
    monkeypatch.setitem(COMPONENT_WEIGHTS, ScoreComponent.EXECUTION, 26)
    with pytest.raises(ScoreTableError, match="sum to 101"):
        scoring._check_weight_table()

    monkeypatch.setitem(COMPONENT_WEIGHTS, ScoreComponent.EXECUTION, -1)
    with pytest.raises(ScoreTableError, match="negative"):
        scoring._check_weight_table()


def test_a_component_without_a_weight_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    table = dict(COMPONENT_WEIGHTS)
    del table[ScoreComponent.REGRESSION]
    monkeypatch.setattr(scoring, "COMPONENT_WEIGHTS", table)
    with pytest.raises(ScoreTableError, match="no weight for"):
        scoring._check_weight_table()


# ---------------------------------------------------------------------------
# 2. the arithmetic
# ---------------------------------------------------------------------------


def test_every_component_passing_scores_one_hundred() -> None:
    score = score_components(one_passing_check_per_component())
    assert score.total == TOTAL_POINTS
    assert score.max_total == TOTAL_POINTS


def test_the_total_is_the_sum_of_the_rows() -> None:
    score = score_components(one_passing_check_per_component())
    assert score.total == sum(row.points for row in score.components)


def test_a_component_with_no_evidence_scores_zero() -> None:
    """The acceptance criterion, in its bare form."""
    score = score_components([])
    assert score.total == 0
    for row in score.components:
        assert row.points == 0
        assert row.checks_executed == 0
        assert row.evidence == ()
        assert row.detail == NO_EVIDENCE_DETAIL


@pytest.mark.parametrize("dropped", list(ScoreComponent))
def test_dropping_one_component_costs_exactly_its_weight(dropped: ScoreComponent) -> None:
    """No default fills the gap: the row goes to zero and the total drops by its weight."""
    checks = [c for c in one_passing_check_per_component() if c.component is not dropped]
    score = score_components(checks)

    row = score.component(dropped)
    assert row.points == 0
    assert not row.has_evidence
    assert row.detail == NO_EVIDENCE_DETAIL
    assert score.total == TOTAL_POINTS - COMPONENT_WEIGHTS[dropped]
    assert score.components_without_evidence == (dropped,)


def test_partial_credit_is_proportional_and_rounded_down() -> None:
    checks = [
        executed("e1", ScoreComponent.EXECUTION),
        executed("e2", ScoreComponent.EXECUTION),
        executed("e3", ScoreComponent.EXECUTION),
        executed("e4", ScoreComponent.EXECUTION, exit_code=1),
    ]
    row = score_components(checks).component(ScoreComponent.EXECUTION)
    # 25 * 3/4 = 18.75. A point that was not earned is not awarded.
    assert row.points == 18
    assert (row.checks_passed, row.checks_executed) == (3, 4)


def test_a_failing_check_costs_its_share() -> None:
    checks = [executed("e1", ScoreComponent.EXECUTION, exit_code=1)]
    row = score_components(checks).component(ScoreComponent.EXECUTION)
    assert row.points == 0
    assert row.has_evidence, "it ran and failed; that is different from never running"
    assert row.detail == "0/1 checks passed."


def test_a_timed_out_check_did_not_pass() -> None:
    checks = [executed("e1", ScoreComponent.EXECUTION, exit_code=0, timed_out=True)]
    assert score_components(checks).component(ScoreComponent.EXECUTION).points == 0


def test_every_row_links_to_the_check_ids_and_evidence_that_produced_it() -> None:
    checks = [
        executed("registration:a", ScoreComponent.REGISTRATION_AND_DISCOVERY),
        executed("registration:b", ScoreComponent.REGISTRATION_AND_DISCOVERY, exit_code=2),
    ]
    row = score_components(checks).component(ScoreComponent.REGISTRATION_AND_DISCOVERY)
    assert row.check_ids == ("registration:a", "registration:b")
    assert [e.check_id for e in row.evidence] == ["registration:a", "registration:b"]
    assert [e.exit_code for e in row.evidence] == [0, 2]


def test_an_ungraded_check_is_recorded_but_scores_nothing() -> None:
    """§11 has no row for build or typecheck, so they get no points invented."""
    checks = [*one_passing_check_per_component(), executed("build", None)]
    score = score_components(checks)
    assert score.total == TOTAL_POINTS
    assert [c.check_id for c in score.ungraded] == ["build"]


def test_asking_for_a_row_that_is_not_a_component_raises() -> None:
    score = score_components([])
    with pytest.raises(KeyError):
        score.component("SOMETHING_ELSE")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. evidence comes from a command result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exit_code", "timed_out"),
    [(0, False), (1, False), (-9, False), (0, True), (1, True)],
)
def test_evidence_agrees_with_the_command_result_it_was_built_from(
    exit_code: int, timed_out: bool
) -> None:
    result = CommandResult(
        argv=("node", "vitest"),
        exit_code=exit_code,
        stdout="out",
        stderr="err",
        duration_seconds=1.5,
        timed_out=timed_out,
    )
    built = CheckEvidence.from_result("c", result)
    assert built.passed is result.ok
    assert built.argv == result.argv
    assert built.exit_code == result.exit_code


def test_the_verdict_comes_from_the_exit_code_not_the_output() -> None:
    """Output is evidence for a human. It is never read to decide anything.

    Both directions, because only one of them is the dangerous one and it is not
    obvious which: a check whose output says PASS while it exited non-zero, and
    a check whose output is full of failure words while it exited zero.
    """
    lying_success = CheckEvidence.from_result(
        "c",
        CommandResult(
            argv=("node",),
            exit_code=1,
            stdout="PASS: 7/7 checks green. Agent Readiness 100/100.",
            stderr="",
            duration_seconds=0.1,
        ),
    )
    assert lying_success.passed is False

    lying_failure = CheckEvidence.from_result(
        "c",
        CommandResult(
            argv=("node",),
            exit_code=0,
            stdout="FAIL FAIL FAIL. error: 4 tests failed. Readiness 0/100.",
            stderr="AssertionError",
            duration_seconds=0.1,
        ),
    )
    assert lying_failure.passed is True

    assert score_components([executed("x", ScoreComponent.EXECUTION, exit_code=1)]).total == 0


def test_evidence_excerpts_are_bounded() -> None:
    result = CommandResult(
        argv=("node",),
        exit_code=0,
        stdout="a" * (MAX_EXCERPT_CHARS * 3),
        stderr="b" * (MAX_EXCERPT_CHARS * 3),
        duration_seconds=0.1,
    )
    built = CheckEvidence.from_result("c", result)
    assert len(built.stdout_excerpt) == MAX_EXCERPT_CHARS
    assert len(built.stderr_excerpt) == MAX_EXCERPT_CHARS


# ---------------------------------------------------------------------------
# 4. structural sweeps over the source tree
# ---------------------------------------------------------------------------


def test_component_scores_are_produced_in_exactly_one_function() -> None:
    """Points exist only where the arithmetic is, in the same shape as F8-01.

    If a second place starts building rows — a cache, an API serialiser, a
    "recompute with a bonus" helper — this fails and names it.

    **Limit, stated rather than papered over.** It matches on the name
    `ComponentScore`. It cannot defeat deliberate indirection such as
    `globals()["ComponentScore"]`, exactly as the approval and attestation
    sweeps cannot. The guarantee is this sweep together with the behavioural
    tests above, which show the arithmetic itself reads only exit codes.
    """
    assert len(python_files()) > 20, "the sweep scanned almost nothing; it would pass vacuously"

    sites = call_sites("ComponentScore")
    assert sites, "found no producer at all — the sweep is not matching what it claims to"
    assert {(path, function) for path, function, _ in sites} == {
        ("mcpforge/orchestration/scoring.py", "score_components")
    }, "component scores are produced in more than one place: " + str(sorted(sites))


def _zero_input_tool() -> WebMCPTool:
    """A legal tool with no inputs. `WebMCPTool.inputs` defaults to empty."""
    return WebMCPTool(
        name="list_rooms",
        title="List every room",
        description="Return the full room list. Takes no arguments.",
        inputs=[],
        output_description="Every room.",
        risk=RiskClass.READ,
        approval_required=False,
        source=SourceBinding(
            module="@/lib/rooms",
            symbol="listRooms",
            call_style=CallStyle.OBJECT,
            parameters=[],
        ),
        evidence=[Evidence(path="src/lib/rooms.ts", symbol="listRooms")],
    )


def test_a_tool_with_no_inputs_has_no_rejection_check() -> None:
    """No inputs means no wrong-type case, so no ERROR_HANDLING evidence.

    `rejection_test_name` returned `""` for such a tool, which passed the
    caller's `is not None` guard and became `-t ''` — and vitest treats an empty
    filter as matching everything, so the *execution* test's green result was
    scored as ERROR_HANDLING. A zero-input tool collected the full 10 points for
    a check the generator never emitted.
    """
    tool = _zero_input_tool()
    assert rejection_test_name(tool) is None, (
        "a falsy-but-not-None name passes an `is not None` guard and becomes an "
        "empty vitest filter, which matches every test in the file"
    )

    toolset = WebMCPToolset(tools=[tool])
    planned = plan_checks(
        toolset,
        ApplicationFacts(scripts=frozenset({"test", "typecheck", "build"}), vitest_available=True),
        app_dir=".",
    )
    error_handling = [c for c in planned if c.check_id == "error_handling:list_rooms"]
    assert len(error_handling) == 1, "the absent check should be recorded, not omitted"
    assert error_handling[0].command is None
    assert error_handling[0].skip_reason == NO_INPUTS_SKIP_REASON

    # Redundant defence, deliberately kept and deliberately labelled. The
    # assertions above are what pin the behaviour; this loop is unreachable
    # while they hold, and it is positional — it relies on `-t`'s value being
    # last in argv. It is here because an empty filter matches every test in
    # the file, which is the shape of the original defect, not because it is
    # the guard.
    for check in planned:
        if check.command is not None:
            assert "-t" not in check.command.argv or check.command.argv[-1] != "", (
                f"empty vitest filter in {check.check_id}: matches every test in the file"
            )


def test_the_generator_emits_no_rejection_test_for_a_zero_input_tool() -> None:
    """The other half: the plan matches what the generator actually writes."""
    toolset = WebMCPToolset(tools=[_zero_input_tool()])
    files = {change.path: change.contents for change in generate_patch(toolset).files}
    test_files = [body for path, body in files.items() if path.endswith(".test.ts")]
    assert test_files, "no generated test file to check"
    for body in test_files:
        assert "rejects the wrong type" not in body
        assert 'it("", ' not in body, "an empty test name was emitted"


def test_no_module_assembles_evidence_from_a_mapping() -> None:
    """No backend module builds an `ExecutedCheck` with a dict for `evidence`.

    The companion to `test_check_evidence_is_constructed_in_exactly_one_place`,
    and it exists because that sweep alone is not the guarantee it was once
    described as: pydantic coerces a mapping into `CheckEvidence`, so a
    hand-assembled dict becomes a passing record with **no call site to find**.
    Measured — such a dict scored the full 25 EXECUTION points.

    Coercion stays allowed on purpose, because `ValidationReport` must
    round-trip through serialisation. So the rule is enforced over source
    rather than at runtime.

    **Stated bound**, in the style this repository uses elsewhere: this matches
    a dict literal in the `evidence` position of a call named `ExecutedCheck`,
    written either bare or attribute-qualified. It does not defeat a dict built
    elsewhere and passed in as a variable, an aliased import, or `getattr`. No
    name-based check can.

    An earlier version matched only the bare name, so
    `scoring.ExecutedCheck(evidence={...})` passed the whole file — the bound
    above was written before the matcher could honour it.
    """
    offenders: list[str] = []
    scanned = 0
    for path in python_files():
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            # Both spellings, matching `tests.structure.call_sites`. Matching only the
            # bare name missed `scoring.ExecutedCheck(evidence={...})` after a
            # module-level import — not deliberate indirection, just the other
            # ordinary way to write the call, and the docstring claimed to
            # catch it.
            func = node.func
            matched = (isinstance(func, ast.Name) and func.id == "ExecutedCheck") or (
                isinstance(func, ast.Attribute) and func.attr == "ExecutedCheck"
            )
            if not matched:
                continue
            for keyword in node.keywords:
                if keyword.arg == "evidence" and isinstance(keyword.value, ast.Dict):
                    offenders.append(f"{path.name}:{keyword.value.lineno}")

    assert scanned > 20, "the sweep read almost nothing; it would report green over anything"
    assert not offenders, f"evidence assembled from a mapping: {offenders}"


def test_a_mapping_is_still_coerced_so_the_report_round_trips() -> None:
    """The bound above, asserted rather than merely described.

    If this ever starts raising, the sweep is no longer the only thing standing
    between a hand-assembled record and a score, and the docstrings that say so
    need to change with it.
    """
    check = ExecutedCheck(
        check_id="execution:search_rooms",
        component=ScoreComponent.EXECUTION,
        description="round-tripped",
        evidence={  # type: ignore[arg-type]
            "check_id": "execution:search_rooms",
            "argv": [],
            "exit_code": 0,
            "timed_out": False,
            "duration_seconds": 0.0,
        },
    )
    assert isinstance(check.evidence, CheckEvidence)


def test_check_evidence_is_constructed_in_exactly_one_place() -> None:
    """Evidence is what a score is made of, so it has the same rule.

    `from_result` takes a `CommandResult`, so this is what makes every point in
    the report traceable to a command the executor actually ran.
    """
    assert len(python_files()) > 20, "the sweep scanned almost nothing"
    sites = call_sites("CheckEvidence")
    assert sites, "found no construction at all — the sweep is not matching"
    assert {(path, function) for path, function, _ in sites} == {
        ("mcpforge/orchestration/scoring.py", "from_result")
    }, "evidence is constructed in more than one place: " + str(sorted(sites))


#: Names that mean a module is talking to Gemini. Matched as identifiers in the
#: AST, never as text, so a docstring explaining the rule cannot satisfy it and
#: a comment cannot defeat it — the failure mode that cost `F8-02a` three review
#: rounds, where a comment mentioning `--require-hashes` satisfied the test for
#: the flag.
GEMINI_NAMES = frozenset(
    {
        "GeminiProvider",
        "GenerationRequest",
        "generate_structured",
        "generate_text",
        "system_instruction",
        "build_prompt",
        "untrusted",
    }
)

#: Names that mean a module is touching the readiness score.
SCORING_NAMES = frozenset(
    {
        "COMPONENT_WEIGHTS",
        "CheckEvidence",
        "ComponentScore",
        "ExecutedCheck",
        "ReadinessScore",
        "ScoreComponent",
        "ValidationReport",
        "score_components",
    }
)


def _referenced_names(path: Path) -> set[str]:
    """Identifiers a module actually uses. Strings and comments are not names."""
    used: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            used.add(node.name)
        elif isinstance(node, ast.alias):
            used.add(node.asname or node.name.rsplit(".", 1)[-1])
    return used


def _talks_to_gemini(path: Path) -> bool:
    if any(module.startswith("mcpforge.gemini") for _, module in imported_modules(path)):
        return True
    return bool(_referenced_names(path) & GEMINI_NAMES)


def _touches_the_score(path: Path) -> bool:
    if any(
        module.startswith("mcpforge.orchestration.scoring") for _, module in imported_modules(path)
    ):
        return True
    return bool(_referenced_names(path) & SCORING_NAMES)


def test_no_module_both_prompts_gemini_and_touches_the_score() -> None:
    """ "Gemini is never asked for a score", as a property of the whole backend.

    Asking a model for a score needs two things in one place: a way to reach the
    model, and the score to put the answer into. This sweep asserts no backend
    module has both. It holds however the request is spelled — a new agent, a
    helper, a second provider — because it matches on the names any of those
    would have to use.

    **Limits, stated.** (a) It matches on names, so it cannot defeat deliberate
    indirection, exactly like the attestation and approval sweeps. (b) It cannot
    stop a module from asking a model for a number and handing it to another
    module as a plain `int`; what forbids *that* is
    `test_component_scores_are_produced_in_exactly_one_function` plus
    `test_check_evidence_is_constructed_in_exactly_one_place`, which together
    mean every point comes from a `CommandResult`. The guarantee is the
    conjunction, not this test alone.
    """
    files = python_files()
    assert len(files) > 20, "the sweep scanned almost nothing; it would pass vacuously"

    gemini = {str(p.relative_to(SRC)) for p in files if _talks_to_gemini(p)}
    scored = {str(p.relative_to(SRC)) for p in files if _touches_the_score(p)}

    # Self-guards: a sweep where either side matched nothing would report green
    # while checking nothing at all.
    assert gemini, "no module appears to reach Gemini — the matcher is broken"
    assert scored, "no module appears to touch the score — the matcher is broken"

    assert not (gemini & scored), (
        "these modules can both reach Gemini and touch the readiness score, which is "
        f"exactly what F8-04 forbids: {sorted(gemini & scored)}"
    )


def test_the_validator_module_cannot_reach_gemini() -> None:
    """The narrow version of the sweep above, aimed at agent 5 itself."""
    path = SRC / "mcpforge" / "agents" / "validator.py"
    modules = [module for _, module in imported_modules(path)]
    assert not [m for m in modules if m.startswith("mcpforge.gemini")]
    assert not (_referenced_names(path) & GEMINI_NAMES)

    tree = ast.parse(path.read_text())
    validator = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "Validator"
    )
    assert validator.bases == [], (
        "Validator has a base class. `Agent` is the only base that reaches the provider, "
        "and agent 5 must not be able to."
    )


def test_the_validator_is_constructed_with_an_executor_and_nothing_else() -> None:
    import inspect

    parameters = list(inspect.signature(Validator.__init__).parameters)
    assert parameters == ["self", "executor", "timeout_seconds"]


# ---------------------------------------------------------------------------
# 5. the check suite
# ---------------------------------------------------------------------------


def test_the_suite_produces_a_check_for_every_scored_component(
    toolset: WebMCPToolset, facts: ApplicationFacts
) -> None:
    planned = plan_checks(toolset, facts)
    covered = {check.component for check in planned if check.component is not None}
    assert covered == set(ScoreComponent)


def test_a_gated_tool_is_checked_for_authorization_and_a_read_tool_for_execution(
    toolset: WebMCPToolset, facts: ApplicationFacts
) -> None:
    planned = {check.check_id: check for check in plan_checks(toolset, facts)}
    assert "authorization:cancel_reservation" in planned
    assert "execution:cancel_reservation" not in planned, (
        "a gated tool must not be scored for executing; it is supposed to refuse"
    )
    assert "execution:search_rooms" in planned
    assert "authorization:search_rooms" not in planned


def _named_tests_in(contents: str) -> set[str]:
    return set(re.findall(r'\bit\("([^"]+)"', contents))


def test_every_targeted_test_name_is_one_the_generator_actually_emits(
    toolset: WebMCPToolset, facts: ApplicationFacts
) -> None:
    """The `-t` filters must name tests that exist in the generated files.

    A stale filter is caught here, and that matters more than it looks:
    `passWithNoTests: false` governs whether a test *file* matched, not whether
    a `-t` name filter matched anything inside one. A filter that matches
    nothing in a file that exists marks every test skipped and exits **0** —
    measured in `test_a_filter_that_matches_no_test_yields_no_evidence`. The
    runtime guard for that is `vitest_ran_a_test`; this test stops the drift
    reaching runtime at all, and says *why* rather than leaving an empty run.
    """
    patch_files = {change.path: change.contents for change in generate_patch(toolset).files}
    targeted = 0
    for check in plan_checks(toolset, facts):
        if check.command is None:
            continue
        argv = list(check.command.argv)
        if "-t" not in argv:
            continue
        targeted += 1
        test_file = argv[argv.index("--config") + 2]
        name = argv[argv.index("-t") + 1]
        assert test_file in patch_files, f"{check.check_id} targets a file the patch does not emit"
        assert name in _named_tests_in(patch_files[test_file]), (
            f"{check.check_id} filters on {name!r}, which the generated file does not define"
        )
    assert targeted >= 4, "no targeted checks were examined; this test would pass vacuously"


def test_every_command_is_an_argument_array_from_the_allowlist(
    toolset: WebMCPToolset, facts: ApplicationFacts
) -> None:
    """03_SECURITY_ACCESS.md §3: an allowlisted executable, never a shell string."""
    planned = [c for c in plan_checks(toolset, facts) if c.command is not None]
    assert planned
    for check in planned:
        command = check.command
        assert command is not None
        assert isinstance(command.argv, tuple)
        assert command.argv[0] in ALLOWED_EXECUTABLES, check.check_id
        for argument in command.argv:
            assert not any(ch in argument for ch in ";|&$`\n"), (
                f"{check.check_id} has a shell metacharacter in {argument!r}"
            )


def test_ui_synchronization_is_planned_and_honestly_skipped(
    toolset: WebMCPToolset, facts: ApplicationFacts
) -> None:
    """Not omitted, not defaulted: planned, unexecutable, and explained."""
    ui = [
        c for c in plan_checks(toolset, facts) if c.component is ScoreComponent.UI_SYNCHRONIZATION
    ]
    assert len(ui) == 1
    assert ui[0].command is None
    assert ui[0].skip_reason == UI_SYNCHRONIZATION_SKIP_REASON


def test_a_missing_script_is_skipped_rather_than_invented(toolset: WebMCPToolset) -> None:
    bare = ApplicationFacts(scripts=frozenset(), vitest_available=True)
    planned = {check.check_id: check for check in plan_checks(toolset, bare)}
    for check_id in ("regression", "typecheck", "build"):
        assert planned[check_id].command is None
        assert "declares no" in (planned[check_id].skip_reason or "")


def test_missing_vitest_skips_every_check_that_needs_it(toolset: WebMCPToolset) -> None:
    without = ApplicationFacts(scripts=frozenset({"typecheck"}), vitest_available=False)
    planned = plan_checks(toolset, without)
    runnable = [c for c in planned if c.command is not None]
    assert [c.check_id for c in runnable] == ["typecheck"]
    assert all(
        VITEST_CLI in (c.skip_reason or "")
        for c in planned
        if c.check_id.startswith("registration:")
    )


def test_a_planned_check_cannot_be_both_runnable_and_skipped() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        PlannedCheck(check_id="x", component=None, description="x")
    with pytest.raises(ValueError, match="exactly one"):
        PlannedCheck(
            check_id="x",
            component=None,
            description="x",
            command=Command(argv=("node",)),
            skip_reason="also skipped",
        )


# ---------------------------------------------------------------------------
# 6. the harness
# ---------------------------------------------------------------------------


def test_the_harness_is_never_part_of_the_patch(toolset: WebMCPToolset) -> None:
    """It is a validation artefact. It must not reach the developer's repository."""
    patch_paths = {change.path for change in generate_patch(toolset).files}
    harness_paths = set(harness_files(toolset))
    assert harness_paths
    assert not (patch_paths & harness_paths)
    assert not [p for p in patch_paths if p.startswith(HARNESS_DIR)]
    assert HARNESS_CONFIG not in patch_paths


def test_the_harness_imports_the_module_the_generator_emits(toolset: WebMCPToolset) -> None:
    """One source for the path. A harness importing a moved module is a red check
    that says nothing about the application."""
    assert alias_specifier("src/webmcp/register.ts") == "@/webmcp/register"
    with pytest.raises(ValidatorError):
        alias_specifier("lib/register.ts")

    files = harness_files(toolset)
    generated = {change.path for change in generate_patch(toolset).files}
    for path, contents in files.items():
        if path == HARNESS_CONFIG:
            continue
        assert 'from "@/webmcp/register"' in contents
    assert "src/webmcp/register.ts" in generated


def test_the_harness_names_every_tool_it_was_built_for(toolset: WebMCPToolset) -> None:
    files = harness_files(toolset)
    for tool in toolset.tools:
        registration = files[f"{HARNESS_DIR}/{tool.handler_name}.registration.test.mts"]
        schema = files[f"{HARNESS_DIR}/{tool.handler_name}.schema.test.mts"]
        assert f'tools["{tool.name}"]' in registration
        assert f'tools["{tool.name}"]' in schema
        for prop in tool.inputs:
            assert f'"{prop.name}"' in schema


def _workspace(root: Path) -> Workspace:
    return Workspace(id="w", root=root.resolve(), trust_level=_TrustLevel.DEVELOPMENT_ISOLATION)


def test_the_harness_is_written_inside_the_workspace(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    validator = Validator(_NullExecutor())
    written = validator.write_harness(_workspace(tmp_path), toolset, app_dir="app")
    assert written
    for relative in written:
        assert (tmp_path / "app" / relative).is_file()


def test_a_harness_path_that_escapes_the_workspace_is_refused(
    tmp_path: Path, toolset: WebMCPToolset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The jail is `provider.resolve_inside`, and the harness writer goes through it.

    Proved by breaking the real thing: point the harness directory outside the
    workspace and observe the refusal rather than a file appearing in the parent.
    """
    monkeypatch.setattr("mcpforge.agents.validator.HARNESS_DIR", "../../escaped")
    with pytest.raises(PathEscapeError):
        Validator(_NullExecutor()).write_harness(_workspace(tmp_path), toolset)
    assert not (tmp_path.parent.parent / "escaped").exists()


# ---------------------------------------------------------------------------
# 7. running the suite, against a fake executor
# ---------------------------------------------------------------------------


class _NullExecutor:
    """Enough of `SecureExecutionProvider` to construct a Validator."""

    @property
    def trust_level(self) -> _TrustLevel:
        return _TrustLevel.DEVELOPMENT_ISOLATION

    async def create_workspace(self, spec: WorkspaceSpec) -> Workspace:  # pragma: no cover
        raise NotImplementedError

    async def run(self, workspace: Workspace, command: Command) -> CommandResult:
        raise NotImplementedError

    async def attestation(self) -> None:
        return None

    async def destroy(self, workspace: Workspace) -> None:  # pragma: no cover
        return None


class ScriptedExecutor(_NullExecutor):
    """Returns the exit code and output a test asks for, per check.

    Deliberately not a mock of the validator's logic: it answers with a real
    `CommandResult`, which is the only thing the validator is allowed to read.
    """

    def __init__(
        self,
        *,
        exit_codes: dict[str, int] | None = None,
        stdout: str = "",
        stderr: str = "",
        raises: dict[str, Exception] | None = None,
        tests_ran: bool = True,
    ) -> None:
        self.tests_ran = tests_ran
        self.exit_codes = exit_codes or {}
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises or {}
        self.commands: list[Command] = []

    def _key(self, command: Command) -> str:
        argv = list(command.argv)
        if argv[0] == "npm":
            return argv[-1]
        target = argv[argv.index("--config") + 2]
        name = argv[argv.index("-t") + 1] if "-t" in argv else None
        return f"{target}::{name}"

    async def run(self, workspace: Workspace, command: Command) -> CommandResult:
        self.commands.append(command)
        key = self._key(command)
        if key in self.raises:
            raise self.raises[key]
        return CommandResult(
            argv=command.argv,
            exit_code=self.exit_codes.get(key, 0),
            stdout=self._stdout_for(command),
            stderr=self.stderr,
            duration_seconds=0.2,
        )

    def _stdout_for(self, command: Command) -> str:
        """A name-filtered run reports how many tests it ran, as vitest does.

        The validator treats a `-t` run that exits 0 without executing a test as
        an absence rather than a pass, because vitest exits 0 when a name filter
        matches nothing. A fake that emits no summary line therefore describes a
        run no real vitest produces, and every targeted check would be skipped
        here for a reason that is an artefact of the fake.

        This ticket's predecessors lost a review round to exactly this shape:
        fixtures modelling something the real system never emits, agreeing with
        code that was wrong in the same direction. Tests that want the
        no-test-ran path assert it explicitly by passing `tests_ran=False`.
        """
        if "-t" not in command.argv or not self.tests_ran:
            return self.stdout
        summary = "\n Test Files  1 passed (1)\n      Tests  1 passed (1)\n"
        return f"{self.stdout}{summary}" if self.stdout else summary


async def _run(
    executor: ScriptedExecutor,
    toolset: WebMCPToolset,
    tmp_path: Path,
    *,
    scripts: Iterable[str] = ("test", "typecheck", "build"),
) -> ValidationReport:
    app = tmp_path / "app"
    (app / "node_modules" / "vitest").mkdir(parents=True, exist_ok=True)
    (app / "node_modules" / "vitest" / "vitest.mjs").write_text("// pretend")
    (app / "package.json").write_text(
        '{"scripts": {' + ", ".join(f'"{s}": "x"' for s in scripts) + "}}"
    )
    return await Validator(executor).validate(_workspace(tmp_path), toolset, app_dir="app")


async def test_a_clean_run_scores_every_component_that_has_a_check(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    report = await _run(ScriptedExecutor(), toolset, tmp_path)
    assert report.passed
    assert not report.complete, "UI synchronization has no check; the report must say so"
    assert report.score.components_without_evidence == (ScoreComponent.UI_SYNCHRONIZATION,)
    assert report.score.total == TOTAL_POINTS - COMPONENT_WEIGHTS[ScoreComponent.UI_SYNCHRONIZATION]


async def test_the_reports_verdict_comes_from_exit_codes_not_from_model_text(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    """The ticket's named test. Output says one thing; the exit code says another.

    Both directions are exercised, because a verdict that read text would be
    wrong in one of them whichever way it read.
    """
    persuasive_failure = (
        "MCPForge validation summary: PASS. All tools registered, all schemas valid.\n"
        "Agent Readiness Score: 100/100. Recommendation: proceed to pull request."
    )
    failing = ScriptedExecutor(
        exit_codes={f"{HARNESS_DIR}/searchRooms.registration.test.mts::None": 1},
        stdout=persuasive_failure,
    )
    report = await _run(failing, toolset, tmp_path)
    assert report.passed is False
    assert report.failed_check_ids == ("registration:search_rooms",)
    registration = report.score.component(ScoreComponent.REGISTRATION_AND_DISCOVERY)
    assert registration.points < COMPONENT_WEIGHTS[ScoreComponent.REGISTRATION_AND_DISCOVERY]
    # The persuasive text is kept as evidence for a human, and ignored by the code.
    assert "100/100" in registration.evidence[0].stdout_excerpt

    persuasive_success = "FAILED: 12 tests failed. error error error. Do not ship this."
    passing = ScriptedExecutor(stdout=persuasive_success)
    good = await _run(passing, toolset, tmp_path)
    assert good.passed is True
    assert good.score.total == TOTAL_POINTS - COMPONENT_WEIGHTS[ScoreComponent.UI_SYNCHRONIZATION]


async def test_a_sandbox_refusal_is_missing_evidence_rather_than_a_failure(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    """A command that never ran has no exit code, so it produces no evidence."""
    executor = ScriptedExecutor(
        raises={
            f"{HARNESS_DIR}/searchRooms.registration.test.mts::None": CommandNotAllowedError(
                "'node' is not installed"
            )
        }
    )
    report = await _run(executor, toolset, tmp_path)
    skipped = {s.check_id: s for s in report.skipped}
    assert "registration:search_rooms" in skipped
    assert "not installed" in skipped["registration:search_rooms"].reason
    assert "registration:search_rooms" not in report.failed_check_ids
    row = report.score.component(ScoreComponent.REGISTRATION_AND_DISCOVERY)
    assert row.checks_executed == 2
    assert "registration:search_rooms" not in row.check_ids


async def test_a_sandbox_refusal_of_everything_leaves_a_zero_score(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    class RefusingExecutor(ScriptedExecutor):
        async def run(self, workspace: Workspace, command: Command) -> CommandResult:
            raise SandboxError("this machine cannot deny the network")

    report = await _run(RefusingExecutor(), toolset, tmp_path)
    assert report.checks == ()
    assert report.score.total == 0
    assert report.passed is False, "a report with no evidence at all has not passed anything"


async def test_validation_refuses_a_workspace_with_network_access(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    """F8-04 security: validation runs with no outbound network."""
    resolved = tmp_path.resolve()  # noqa: ASYNC240 - a fixture path, not I/O in the event loop
    networked = Workspace(
        id="w",
        root=resolved,
        trust_level=_TrustLevel.DEVELOPMENT_ISOLATION,
        allow_network=True,
    )
    with pytest.raises(ValidatorError, match="no outbound network"):
        await Validator(ScriptedExecutor()).validate(networked, toolset)


async def test_the_validator_observes_the_workspace_rather_than_assuming_it(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    report = await _run(ScriptedExecutor(), toolset, tmp_path, scripts=("typecheck",))
    skipped = {s.check_id for s in report.skipped}
    assert {"regression", "build", "ui_synchronization"} == skipped
    assert report.score.component(ScoreComponent.REGRESSION).points == 0
    assert report.score.component(ScoreComponent.REGRESSION).detail == NO_EVIDENCE_DETAIL


async def test_the_report_round_trips_through_serialisation(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    """It is persisted as a VALIDATION artifact, so it has to survive JSON."""
    report = await _run(ScriptedExecutor(), toolset, tmp_path)
    restored = ValidationReport.model_validate_json(report.model_dump_json())
    assert restored.score.total == report.score.total
    assert restored.passed == report.passed
    assert "passed" not in report.model_dump(), (
        "the verdict must stay derived; a stored field could disagree with its evidence"
    )


# ---------------------------------------------------------------------------
# 8. the integration run, against the real fixture
# ---------------------------------------------------------------------------


INTEGRATION_ENV = "MCPFORGE_VALIDATION_INTEGRATION_REQUIRED"

#: `next build` and `vitest` both instantiate WebAssembly, and V8 reserves a
#: large virtual address range per instance. Under the executor's default
#: `RLIMIT_AS` of 2 GB every Node toolchain command dies with
#: "Cannot allocate Wasm memory for new instance". The limit is an address-space
#: limit, not a resident-memory limit, so raising it does not let a job use more
#: RAM; it lets Node reserve the space it always reserves.
VALIDATION_ADDRESS_SPACE_MB = 256 * 1024


def _skip_or_fail(message: str) -> None:
    if os.environ.get(INTEGRATION_ENV) == "1":
        pytest.fail(f"{INTEGRATION_ENV}=1 and {message}")
    pytest.skip(message)


def _require_toolchain() -> None:
    if not (NODE_MODULES / "vitest" / "vitest.mjs").is_file():
        _skip_or_fail("node_modules/vitest is not installed; run npm ci at the repository root")
    for executable in ("/usr/bin/node", "/usr/bin/npm"):
        if not Path(executable).exists():
            _skip_or_fail(f"{executable} is missing; the sandbox PATH cannot reach the toolchain")
    if not DEMO_APP.is_dir():
        _skip_or_fail("the demo fixture is missing")


def _materialise(app: Path, toolset: WebMCPToolset) -> None:
    """The real fixture, the real generated patch, and a real dependency tree."""
    shutil.copytree(
        DEMO_APP,
        app,
        ignore=shutil.ignore_patterns("node_modules", ".next", "tsconfig.tsbuildinfo"),
    )
    # Hard links, not a symlink: Turbopack refuses a node_modules symlink that
    # points outside the project root, and a full copy is 800 MB.
    subprocess.run(  # noqa: S603
        ["cp", "-al", str(NODE_MODULES), str(app / "node_modules")],  # noqa: S607
        check=True,
    )
    for change in generate_patch(toolset, base_commit="demo").files:
        destination = app / change.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(change.contents)


async def test_the_whole_suite_runs_against_the_demo_fixture(toolset: WebMCPToolset) -> None:
    """The real thing: real patch, real fixture, real sandbox, real exit codes.

    Nothing here is stubbed. vitest genuinely registers the generated tools
    against a stub model context, genuinely calls the fixture's own functions,
    and the gated tool genuinely refuses to act.
    """
    _require_toolchain()

    executor = DevelopmentSecureExecutor(memory_mb=VALIDATION_ADDRESS_SPACE_MB, cpu_seconds=1200)
    async with executor.workspace(WorkspaceSpec(run_id="f8-04-integration")) as workspace:
        _materialise(workspace.root / "app", toolset)
        report = await Validator(executor, timeout_seconds=900).validate(
            workspace, toolset, app_dir="app"
        )

    failures = [
        f"{c.check_id}: exit {c.evidence.exit_code}\n{c.evidence.stdout_excerpt[-1500:]}"
        for c in report.checks
        if not c.passed
    ]
    assert not failures, "\n\n".join(failures)

    executed_ids = {c.check_id for c in report.checks}
    assert executed_ids == {
        "registration:search_rooms",
        "registration:check_availability",
        "registration:cancel_reservation",
        "schema:search_rooms",
        "schema:check_availability",
        "schema:cancel_reservation",
        "execution:search_rooms",
        "execution:check_availability",
        "authorization:cancel_reservation",
        "error_handling:search_rooms",
        "error_handling:check_availability",
        "error_handling:cancel_reservation",
        "typecheck",
        "build",
    }
    assert report.passed

    # The demo fixture declares no test script and MCPForge has no executable
    # UI-synchronization check, so those two components have no evidence and
    # score zero. That is the acceptance criterion on a real run.
    assert set(report.score.components_without_evidence) == {
        ScoreComponent.UI_SYNCHRONIZATION,
        ScoreComponent.REGRESSION,
    }
    assert report.score.total == TOTAL_POINTS - (
        COMPONENT_WEIGHTS[ScoreComponent.UI_SYNCHRONIZATION]
        + COMPONENT_WEIGHTS[ScoreComponent.REGRESSION]
    )

    for row in report.score.components:
        if row.has_evidence:
            assert row.points == row.weight
            assert row.evidence and all(e.argv[0] in {"node", "npm"} for e in row.evidence)
        else:
            assert row.points == 0
            assert row.detail == NO_EVIDENCE_DETAIL


async def test_a_broken_tool_costs_its_component_points_on_a_real_run(
    toolset: WebMCPToolset,
) -> None:
    """Prove the integration run can go red, by breaking the application itself.

    The generated handler for `search_rooms` is rewritten to throw before it
    reaches the fixture's function. Nothing about the score is touched: the real
    vitest process exits non-zero and the arithmetic follows.
    """
    _require_toolchain()

    executor = DevelopmentSecureExecutor(memory_mb=VALIDATION_ADDRESS_SPACE_MB, cpu_seconds=1200)
    async with executor.workspace(WorkspaceSpec(run_id="f8-04-red")) as workspace:
        app = workspace.root / "app"
        _materialise(app, toolset)

        handler = app / "src" / "webmcp" / "tools" / "searchRooms.ts"
        source = handler.read_text()
        broken = source.replace(
            "  try {",
            '  throw new Error("broken on purpose by the F8-04 test");\n  try {',
            1,
        )
        assert broken != source, "the handler shape changed; this mutation no longer applies"
        handler.write_text(broken)

        report = await Validator(executor, timeout_seconds=900).validate(
            workspace, toolset, app_dir="app"
        )

    assert report.passed is False
    assert "execution:search_rooms" in report.failed_check_ids
    execution = report.score.component(ScoreComponent.EXECUTION)
    assert execution.checks_executed == 2
    assert execution.checks_passed == 1
    assert execution.points == COMPONENT_WEIGHTS[ScoreComponent.EXECUTION] // 2


async def test_a_filter_that_matches_no_test_yields_no_evidence(
    toolset: WebMCPToolset,
) -> None:
    """A `-t` filter matching nothing exits **0**, so the validator must catch it.

    This test was written asserting the opposite — that `passWithNoTests: false`
    covers a name filter — and it failed, which is how the real behaviour was
    found. `passWithNoTests` governs whether a *file* matched; a filter that
    matches nothing inside a file that does exist is a clean, successful,
    empty run.

    That matters because several checks select one generated test by name: a
    renamed or deleted test would otherwise score full marks for a check that
    ran nothing. `Validator.validate` therefore records such a run as skipped
    rather than executed, so the component scores zero for want of evidence
    rather than full marks for an empty success.

    Both halves are asserted here: the measured exit code, so the premise
    cannot silently change under us, and the validator's handling of it.
    """
    _require_toolchain()

    executor = DevelopmentSecureExecutor(memory_mb=VALIDATION_ADDRESS_SPACE_MB, cpu_seconds=1200)
    validator = Validator(executor, timeout_seconds=900)
    async with executor.workspace(WorkspaceSpec(run_id="f8-04-nomatch")) as workspace:
        app = workspace.root / "app"
        _materialise(app, toolset)
        validator.write_harness(workspace, toolset, app_dir="app")

        result = await executor.run(
            workspace,
            Command(
                argv=(
                    "node",
                    VITEST_CLI,
                    "run",
                    "--config",
                    HARNESS_CONFIG,
                    "src/webmcp/tools/searchRooms.test.ts",
                    "-t",
                    "a test name that does not exist anywhere",
                ),
                cwd="app",
                timeout_seconds=300,
                env={"NO_COLOR": "1"},
            ),
        )

        assert result.exit_code == 0, (
            "vitest now fails a name filter that matches nothing. That is stricter "
            "than when this was written, and the guard in Validator.validate may be "
            "redundant — verify before removing it."
        )
        # The precise shape matters and was twice assumed wrongly: vitest does
        # not report an empty run. It finds the file, marks every test in it
        # skipped, and exits 0 — `Tests  2 skipped (2)`. Asserted here so the
        # premise cannot drift silently under a vitest upgrade.
        assert "skipped" in result.stdout, (
            "vitest no longer reports a name-filter miss as skipped tests; "
            f"vitest_ran_a_test's pattern is built on that shape. stdout: {result.stdout[-400:]!r}"
        )
        assert not vitest_ran_a_test(result), (
            "a filter matching nothing reported a test as having run; the liveness "
            "guard reads that summary line and would now accept an empty run"
        )

        # And the same result, seen the way the validator sees it.
        passing = CommandResult(
            argv=result.argv,
            exit_code=0,
            stdout="\n Test Files  1 passed (1)\n      Tests  1 passed (1)\n",
            stderr="",
            duration_seconds=0.1,
        )
        assert vitest_ran_a_test(passing), (
            "a genuine run was not recognised as having run a test; every targeted "
            "check would be discarded as evidence-free"
        )


def test_the_integration_run_can_be_made_mandatory() -> None:
    """The gate itself, so a permanently-skipping suite cannot look green.

    CI sets `MCPFORGE_VALIDATION_INTEGRATION_REQUIRED=1`, which turns every
    prerequisite miss above into a failure.
    """
    assert INTEGRATION_ENV == "MCPFORGE_VALIDATION_INTEGRATION_REQUIRED"
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert f'{INTEGRATION_ENV}: "1"' in workflow
