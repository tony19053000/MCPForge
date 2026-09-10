"""Before/after demonstration — ticket F8-05.

Six groups, and every one of them is written against the *flattering* failure,
because that is the failure this ticket exists to prevent:

1. structural sweeps — a number can only be produced in one place, and a
   missing number has nowhere to come from;
2. the report's own refusals — a run that cites the wrong id, two runs
   contained differently, a change against a side nothing measured;
3. reading a trace — missing, truncated, malformed, and the difference between
   a measured zero and an absence;
4. the runner — identical containment on both sides, refused otherwise;
5. the cross-tier contract — the fixtures the web tier renders are reports this
   backend actually produces, checked against the model rather than against
   themselves;
6. an integration run against the real demo fixture, before and after the real
   generated patch, in the real sandbox.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcpforge.execution.development import DevelopmentSecureExecutor
from mcpforge.execution.provider import (
    AttestationEvidence,
    Command,
    CommandResult,
    PathEscapeError,
    SandboxError,
    TrustLevel,
    Workspace,
    WorkspaceSpec,
)
from mcpforge.generation.nextjs import generate_patch
from mcpforge.models.webmcp import WebMCPToolset
from mcpforge.orchestration.benchmark import (
    ABSENT_REGISTER_PATH,
    HARNESS_CONFIG,
    SCENARIO_PATH,
    TRACE_PATH,
    Absence,
    Benchmarker,
    BenchmarkError,
    BenchmarkReport,
    BenchmarkRun,
    EventKind,
    IntegrationPresence,
    InteractionEvent,
    InteractionMetric,
    Measurement,
    MetricRow,
    RunSide,
    RunTarget,
    SandboxProfile,
    TraceError,
    build_rows,
    harness_files,
    measure_run,
    parse_trace,
    read_trace,
)
from mcpforge.orchestration.scoring import CheckEvidence
from tests.structure import call_sites, imported_modules, python_files
from tests.test_validator import _toolset

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO_APP = REPO_ROOT / "fixtures" / "demo-hotel-app"
NODE_MODULES = REPO_ROOT / "node_modules"
WEB_FIXTURES = REPO_ROOT / "apps" / "web" / "tests" / "fixtures"

STAMP = datetime(2026, 9, 10, 3, 13, 11, tzinfo=UTC)


@pytest.fixture
def toolset() -> WebMCPToolset:
    return _toolset()


# ---------------------------------------------------------------------------
# fixtures — a workspace, a command, a trace
# ---------------------------------------------------------------------------


def workspace(root: Path, *, allow_network: bool = False, identifier: str = "ws") -> Workspace:
    root.mkdir(parents=True, exist_ok=True)
    return Workspace(
        id=identifier,
        root=root.resolve(),
        trust_level=TrustLevel.DEVELOPMENT_ISOLATION,
        allow_network=allow_network,
    )


def command() -> Command:
    return Benchmarker(_NullExecutor()).scenario_command(app_dir="app")


def profile(ws: Workspace) -> SandboxProfile:
    return SandboxProfile.of(ws, command())


def evidence(side: RunSide, *, exit_code: int = 0) -> CheckEvidence:
    return CheckEvidence.from_result(
        f"benchmark:{side.value.lower()}",
        CommandResult(
            argv=command().argv,
            exit_code=exit_code,
            stdout="",
            stderr="",
            duration_seconds=0.5,
        ),
    )


def scenario_trace(
    *,
    tools: Iterable[str] = ("search_rooms", "cancel_reservation"),
    registered: bool,
    presence: IntegrationPresence,
    finished: bool = True,
) -> tuple[InteractionEvent, ...]:
    """A trace in the shape the real scenario writes.

    The real one is exercised by the integration run at the bottom of this file;
    this builds the same event sequence so the pure tests are not describing a
    shape nothing emits.
    """
    at = 1000.0
    events = [InteractionEvent(kind=EventKind.SCENARIO_STARTED, at_ms=at, integration=presence)]

    def add(kind: EventKind, task: str | None = None, detail: str = "") -> None:
        nonlocal at
        at += 1.0
        events.append(InteractionEvent(kind=kind, at_ms=at, task=task, detail=detail))

    for tool in tools:
        add(EventKind.TASK_ATTEMPTED, tool)
        add(EventKind.STEP, tool)
        if registered:
            add(
                EventKind.APPROVAL_REQUIRED
                if tool == "cancel_reservation"
                else EventKind.TASK_COMPLETED,
                tool,
            )
            continue
        add(EventKind.ERROR, tool, "no tool with this name is registered")
        add(EventKind.RETRY, tool)
        add(EventKind.STEP, tool)
        add(EventKind.ERROR, tool, "no tool with this name is registered")

    if finished:
        add(EventKind.SCENARIO_FINISHED)
    return tuple(events)


def run_record(
    side: RunSide,
    *,
    run_id: str | None = None,
    events: tuple[InteractionEvent, ...] = (),
    complete: bool = True,
    note: str = "",
    sandbox: SandboxProfile | None = None,
    integration: IntegrationPresence | None = None,
) -> BenchmarkRun:
    return BenchmarkRun(
        run_id=run_id or f"bench:{side.value.lower()}",
        side=side,
        workspace_id=f"ws-{side.value.lower()}",
        started_at=STAMP,
        completed_at=STAMP,
        sandbox=sandbox or profile(workspace(Path("/tmp"))),  # noqa: S108 - a value, not a file
        evidence=evidence(side),
        events=events,
        trace_complete=complete,
        trace_note=note,
        integration=integration,
    )


def measured_pair() -> tuple[BenchmarkRun, BenchmarkRun]:
    before = run_record(
        RunSide.BEFORE,
        events=scenario_trace(registered=False, presence=IntegrationPresence.ABSENT),
        integration=IntegrationPresence.ABSENT,
    )
    after = run_record(
        RunSide.AFTER,
        events=scenario_trace(registered=True, presence=IntegrationPresence.PRESENT),
        integration=IntegrationPresence.PRESENT,
    )
    return before, after


def report_of(before: BenchmarkRun, after: BenchmarkRun) -> BenchmarkReport:
    return BenchmarkReport(
        benchmark_id="bench",
        sandbox=before.sandbox,
        runs=(before, after),
        rows=build_rows(before, after, recorded_at=STAMP),
    )


class _NullExecutor:
    """Enough of the provider protocol to build a command. Never runs one."""

    @property
    def trust_level(self) -> TrustLevel:
        return TrustLevel.DEVELOPMENT_ISOLATION

    async def create_workspace(self, spec: WorkspaceSpec) -> Workspace:  # pragma: no cover
        raise NotImplementedError

    async def run(self, ws: Workspace, cmd: Command) -> CommandResult:  # pragma: no cover
        raise NotImplementedError

    async def attestation(self) -> AttestationEvidence | None:  # pragma: no cover
        return None

    async def destroy(self, ws: Workspace) -> None:  # pragma: no cover
        return None


class ScriptedExecutor(_NullExecutor):
    """Writes a trace of the test's choosing, then answers with a real result.

    It writes into the workspace exactly where the scenario would, so the code
    under test reads a file rather than a value handed to it.
    """

    def __init__(
        self,
        *,
        traces: dict[str, str] | None = None,
        exit_code: int = 0,
    ) -> None:
        self.traces = traces or {}
        self.exit_code = exit_code
        self.commands: list[tuple[str, Command]] = []

    async def run(self, ws: Workspace, cmd: Command) -> CommandResult:
        self.commands.append((ws.id, cmd))
        body = self.traces.get(ws.id)
        if body is not None:
            (ws.root / (cmd.cwd or ".") / TRACE_PATH).write_text(body, encoding="utf-8")
        return CommandResult(
            argv=cmd.argv,
            exit_code=self.exit_code,
            stdout="",
            stderr="",
            duration_seconds=0.3,
        )


def trace_body(events: Iterable[InteractionEvent]) -> str:
    return "".join(
        json.dumps(event.model_dump(mode="json", exclude_none=True)) + "\n" for event in events
    )


# ---------------------------------------------------------------------------
# 1. structural sweeps
# ---------------------------------------------------------------------------


def test_measurements_are_produced_in_exactly_one_function() -> None:
    """Every displayed number comes from one function, so it has one origin.

    The same rule and the same stated bound as F8-04's `ComponentScore` and
    `CheckEvidence` sweeps: it matches the name `Measurement` in the AST, bare
    or attribute-qualified, and no name-based check defeats deliberate
    indirection.
    """
    assert len(python_files()) > 20, "the sweep scanned almost nothing"
    sites = call_sites("Measurement")
    assert sites, "found no producer at all — the sweep is not matching what it claims to"
    assert {(path, function) for path, function, _ in sites} == {
        ("mcpforge/orchestration/benchmark.py", "_measure")
    }, "measurements are produced in more than one place: " + str(sorted(sites))


def test_no_module_assembles_a_metric_row_from_a_mapping() -> None:
    """The companion sweep, for the same reason F8-04 needed one.

    pydantic coerces a mapping into `Measurement`, so a hand-assembled dict in
    the `before` or `after` position of a `MetricRow` would become a displayed
    number with **no call site for the sweep above to find**. Coercion stays
    allowed, because the report must round-trip through serialisation, so the
    rule is enforced over source.

    **Stated bound**, matching the sweep it mirrors: a dict literal in the
    `before`/`after` keyword position of a call named `MetricRow`, written bare
    or attribute-qualified. A dict built elsewhere and passed as a variable is
    not matched.
    """
    offenders: list[str] = []
    scanned = 0
    for path in python_files():
        scanned += 1
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            matched = (isinstance(func, ast.Name) and func.id == "MetricRow") or (
                isinstance(func, ast.Attribute) and func.attr == "MetricRow"
            )
            if not matched:
                continue
            for keyword in node.keywords:
                if keyword.arg in {"before", "after"} and isinstance(keyword.value, ast.Dict):
                    offenders.append(f"{path.name}:{keyword.value.lineno}")

    assert scanned > 20, "the sweep read almost nothing; it would report green over anything"
    assert not offenders, f"a metric cell assembled from a mapping: {offenders}"


def test_a_mapping_is_still_coerced_so_the_report_round_trips() -> None:
    """The bound above, asserted rather than merely described."""
    before, after = measured_pair()
    report = report_of(before, after)
    restored = BenchmarkReport.model_validate_json(report.model_dump_json())
    assert restored == report


def test_an_absence_has_no_value_field_to_default() -> None:
    """ "Absent rather than defaulted" is the shape of the record, not a habit.

    There is no `value` on `Absence`, so no renderer, serialiser or aggregator
    can read a zero off one. A `value=` passed in is not silently accepted
    either — pydantic's default config would ignore an unknown keyword, and an
    ignored keyword is how a number quietly survives into a payload.
    """
    assert "value" not in Absence.model_fields
    absent = Absence(metric=InteractionMetric.ERRORS, side=RunSide.BEFORE, reason="no trace")
    assert not hasattr(absent, "value")
    assert "value" not in absent.model_dump()


def test_the_benchmark_module_cannot_reach_gemini() -> None:
    """No model produces a number here, and there is no import through which one could."""
    path = next(p for p in python_files() if p.name == "benchmark.py")
    modules = [module for _, module in imported_modules(path)]
    assert not [m for m in modules if "genai" in m or "gemini" in m.lower()], modules
    assert not [m for m in modules if m.startswith("mcpforge.llm")], modules


# ---------------------------------------------------------------------------
# 2. what the report refuses to be
# ---------------------------------------------------------------------------


def test_a_measurement_cannot_cite_a_run_that_is_not_in_the_report() -> None:
    """A run id that names nothing is a citation that traces nowhere."""
    before, after = measured_pair()
    rows = list(build_rows(before, after, recorded_at=STAMP))
    stolen = rows[0].before
    assert isinstance(stolen, Measurement)
    rows[0] = rows[0].model_copy(
        update={"before": stolen.model_copy(update={"run_id": "elsewhere"})}
    )

    with pytest.raises(ValidationError, match="which is not that side's run"):
        BenchmarkReport(
            benchmark_id="bench", sandbox=before.sandbox, runs=(before, after), rows=tuple(rows)
        )


def test_a_report_refuses_two_differently_sandboxed_runs(tmp_path: Path) -> None:
    """F8-05's security requirement, as a refusal to exist."""
    before, after = measured_pair()
    networked = profile(workspace(tmp_path / "b", allow_network=True))
    loosened = after.model_copy(update={"sandbox": networked})

    with pytest.raises(ValidationError, match="sandboxed differently"):
        BenchmarkReport(
            benchmark_id="bench",
            sandbox=before.sandbox,
            runs=(before, loosened),
            rows=build_rows(before, after, recorded_at=STAMP),
        )


def test_a_report_refuses_a_networked_sandbox(tmp_path: Path) -> None:
    """Identical containment is not enough if the containment is not there."""
    networked = profile(workspace(tmp_path / "n", allow_network=True))
    before = run_record(
        RunSide.BEFORE,
        events=scenario_trace(registered=False, presence=IntegrationPresence.ABSENT),
        sandbox=networked,
    )
    after = run_record(
        RunSide.AFTER,
        events=scenario_trace(registered=True, presence=IntegrationPresence.PRESENT),
        sandbox=networked,
    )
    with pytest.raises(ValidationError, match="no outbound network"):
        BenchmarkReport(
            benchmark_id="bench",
            sandbox=networked,
            runs=(before, after),
            rows=build_rows(before, after, recorded_at=STAMP),
        )


def test_a_report_carries_every_metric_exactly_once_in_order() -> None:
    before, after = measured_pair()
    report = report_of(before, after)
    assert tuple(row.metric for row in report.rows) == tuple(InteractionMetric)

    with pytest.raises(ValidationError, match="every metric exactly once"):
        BenchmarkReport(
            benchmark_id="bench",
            sandbox=before.sandbox,
            runs=(before, after),
            rows=report.rows[:-1],
        )


def test_a_change_cannot_be_stated_against_a_side_that_was_not_measured() -> None:
    """No extrapolation: a delta needs two measurements, not one and a zero."""
    row = MetricRow(
        metric=InteractionMetric.ERRORS,
        label=InteractionMetric.ERRORS.label,
        unit=InteractionMetric.ERRORS.unit,
        before=Absence(metric=InteractionMetric.ERRORS, side=RunSide.BEFORE, reason="no trace"),
        after=_measurement(InteractionMetric.ERRORS, RunSide.AFTER, 4.0),
        delta=None,
    )
    assert row.delta is None

    with pytest.raises(ValidationError, match="was not measured on both sides"):
        MetricRow(
            metric=InteractionMetric.ERRORS,
            label=InteractionMetric.ERRORS.label,
            unit=InteractionMetric.ERRORS.unit,
            before=Absence(metric=InteractionMetric.ERRORS, side=RunSide.BEFORE, reason="no trace"),
            after=_measurement(InteractionMetric.ERRORS, RunSide.AFTER, 4.0),
            delta=4.0,
        )


def test_a_change_must_be_the_difference_of_its_two_sides() -> None:
    """A flattering delta is arithmetic that does not follow from the cells."""
    with pytest.raises(ValidationError, match="not the difference of its two sides"):
        MetricRow(
            metric=InteractionMetric.ERRORS,
            label=InteractionMetric.ERRORS.label,
            unit=InteractionMetric.ERRORS.unit,
            before=_measurement(InteractionMetric.ERRORS, RunSide.BEFORE, 6.0),
            after=_measurement(InteractionMetric.ERRORS, RunSide.AFTER, 1.0),
            delta=-99.0,
        )


def _measurement(metric: InteractionMetric, side: RunSide, value: float) -> Measurement:
    """A measurement built by the model directly.

    Only a test does this. Production code reaches `_measure`, which is what
    `test_measurements_are_produced_in_exactly_one_function` enforces — and
    this module is not backend source, so it is outside that sweep.
    """
    return Measurement(
        metric=metric,
        side=side,
        value=value,
        unit=metric.unit,
        run_id=f"bench:{side.value.lower()}",
        recorded_at=STAMP,
        derivation="constructed by a test",
        evidence=evidence(side),
    )


# ---------------------------------------------------------------------------
# 3. reading a trace
# ---------------------------------------------------------------------------


def test_a_missing_trace_measures_nothing_and_defaults_nothing(tmp_path: Path) -> None:
    """The acceptance criterion, at the source: absent, never zero."""
    ws = workspace(tmp_path / "ws")
    (ws.root / "app").mkdir()
    reading = read_trace(ws, app_dir="app")

    assert reading.complete is False
    assert "wrote no trace" in reading.note

    run = run_record(RunSide.BEFORE, complete=False, note=reading.note)
    cells = measure_run(run, recorded_at=STAMP)
    assert set(cells) == set(InteractionMetric)
    assert all(isinstance(cell, Absence) for cell in cells.values())
    assert not [cell for cell in cells.values() if hasattr(cell, "value")]


def test_a_truncated_trace_measures_nothing(tmp_path: Path) -> None:
    """Counting a scenario that stopped halfway produces partial numbers.

    A partial count is indistinguishable from a measurement once it is on
    screen, so it is refused at the point it would be produced.
    """
    ws = workspace(tmp_path / "ws")
    (ws.root / "app").mkdir()
    (ws.root / "app" / TRACE_PATH).write_text(
        trace_body(
            scenario_trace(registered=True, presence=IntegrationPresence.PRESENT, finished=False)
        )
    )

    reading = read_trace(ws, app_dir="app")
    assert reading.complete is False
    assert "did not run to completion" in reading.note
    # The presence flag survives, because the header event was written.
    assert reading.integration is IntegrationPresence.PRESENT


def test_a_malformed_trace_line_is_an_error_not_a_skipped_line() -> None:
    """Skipping an unreadable line yields a count that is silently short."""
    with pytest.raises(TraceError, match="line 2 of the trace is not JSON"):
        parse_trace('{"kind": "step", "at_ms": 1.0}\nnot json at all\n')


def test_an_unknown_event_kind_is_rejected() -> None:
    with pytest.raises(TraceError, match="line 1 of the trace is not an event"):
        parse_trace('{"kind": "invented_kind", "at_ms": 1.0}\n')


def test_a_trace_path_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    ws = workspace(tmp_path / "ws")
    reading = read_trace(ws, app_dir="../../escape")
    assert reading.complete is False
    assert "not inside the workspace" in reading.note


def test_a_measured_zero_is_a_measurement_not_an_absence() -> None:
    """The distinction the whole ticket turns on.

    The before run genuinely reaches no approval point — it found no tools to
    call — so `APPROVAL_POINTS` is zero *and measured*, with a run id and a
    timestamp. That is not the same record as a metric nothing observed, and
    conflating the two in either direction is the defect.
    """
    before, _after = measured_pair()
    cells = measure_run(before, recorded_at=STAMP)

    approvals = cells[InteractionMetric.APPROVAL_POINTS]
    assert isinstance(approvals, Measurement)
    assert approvals.value == 0.0
    assert approvals.run_id == before.run_id
    assert approvals.recorded_at == STAMP

    completed = cells[InteractionMetric.TASKS_COMPLETED]
    assert isinstance(completed, Measurement)
    assert completed.value == 0.0


def test_the_counts_are_the_events_the_trace_actually_holds() -> None:
    """Arithmetic against the trace, not against an expected shape.

    The expected numbers are derived from the event list rather than written
    out, so a hardcoded constant in the producer cannot satisfy this.
    """
    before, after = measured_pair()
    for run in (before, after):
        cells = measure_run(run, recorded_at=STAMP)
        for metric, kind in (
            (InteractionMetric.INTERACTION_STEPS, EventKind.STEP),
            (InteractionMetric.TASKS_ATTEMPTED, EventKind.TASK_ATTEMPTED),
            (InteractionMetric.TASKS_COMPLETED, EventKind.TASK_COMPLETED),
            (InteractionMetric.ERRORS, EventKind.ERROR),
            (InteractionMetric.RETRIES, EventKind.RETRY),
            (InteractionMetric.APPROVAL_POINTS, EventKind.APPROVAL_REQUIRED),
        ):
            cell = cells[metric]
            assert isinstance(cell, Measurement)
            assert cell.value == sum(1 for event in run.events if event.kind is kind), (
                f"{metric.value} on the {run.side.value} run"
            )

    # And the two sides genuinely differ, so no single constant satisfies both.
    left = measure_run(before, recorded_at=STAMP)[InteractionMetric.ERRORS]
    right = measure_run(after, recorded_at=STAMP)[InteractionMetric.ERRORS]
    assert isinstance(left, Measurement) and isinstance(right, Measurement)
    assert left.value != right.value


def test_elapsed_is_the_trace_span_not_the_process_duration() -> None:
    """The number is the agent's interaction, not vitest's start-up cost.

    The command in `evidence` took 0.5s; the scenario inside it did not.
    """
    before, _ = measured_pair()
    cell = measure_run(before, recorded_at=STAMP)[InteractionMetric.ELAPSED_SECONDS]
    assert isinstance(cell, Measurement)

    started = next(e for e in before.events if e.kind is EventKind.SCENARIO_STARTED)
    finished = next(e for e in before.events if e.kind is EventKind.SCENARIO_FINISHED)
    assert cell.value == pytest.approx((finished.at_ms - started.at_ms) / 1000.0)
    assert cell.value != before.evidence.duration_seconds


def test_every_measurement_carries_a_run_id_and_a_timestamp() -> None:
    """The acceptance criterion, stated over the produced records."""
    before, after = measured_pair()
    report = report_of(before, after)
    by_id = {run.run_id: run for run in report.runs}

    assert report.measurements, "the fixture measured nothing; this would pass over anything"
    for measurement in report.measurements:
        assert measurement.run_id in by_id
        assert by_id[measurement.run_id].side is measurement.side
        assert measurement.recorded_at.tzinfo is not None
        assert measurement.evidence.argv == by_id[measurement.run_id].evidence.argv


def test_an_incomplete_run_must_say_why() -> None:
    with pytest.raises(ValidationError, match="must say why it is unusable"):
        run_record(RunSide.BEFORE, complete=False, note="")


# ---------------------------------------------------------------------------
# 4. the runner
# ---------------------------------------------------------------------------


def test_both_sides_receive_the_identical_command() -> None:
    """One builder, so the two runs cannot be given different arguments."""
    runner = Benchmarker(_NullExecutor(), timeout_seconds=42)
    assert runner.scenario_command(app_dir="app") == runner.scenario_command(app_dir="app")
    assert runner.scenario_command(app_dir="app").timeout_seconds == 42


def test_the_harness_is_the_same_on_both_sides(toolset: WebMCPToolset) -> None:
    """`harness_files` takes no side, so nothing about the before run differs.

    Asserted over the signature as well as the output, because "there is no
    parameter for it" is the property, and equal output from one call proves
    nothing on its own.
    """
    import inspect

    assert "side" not in inspect.signature(harness_files).parameters
    files = harness_files(toolset)
    assert set(files) == {HARNESS_CONFIG, ABSENT_REGISTER_PATH, SCENARIO_PATH}


def test_the_harness_is_never_part_of_the_patch(toolset: WebMCPToolset) -> None:
    """The scenario lives in the workspace and never in the developer's repo."""
    paths = {change.path for change in generate_patch(toolset, base_commit="demo").files}
    assert not paths & set(harness_files(toolset))
    assert not [p for p in paths if "__benchmark__" in p or p == TRACE_PATH]


def test_the_harness_cannot_escape_the_workspace(tmp_path: Path, toolset: WebMCPToolset) -> None:
    ws = workspace(tmp_path / "ws")
    with pytest.raises(PathEscapeError):
        Benchmarker(_NullExecutor()).write_harness(ws, toolset, app_dir="../../escaped")
    assert not (tmp_path.parent / "escaped").exists()


async def test_compare_refuses_a_networked_workspace(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    before = RunTarget(workspace(tmp_path / "b", identifier="b"), "app")
    after = RunTarget(workspace(tmp_path / "a", allow_network=True, identifier="a"), "app")
    with pytest.raises(BenchmarkError, match="no outbound network"):
        await Benchmarker(ScriptedExecutor()).compare(before=before, after=after, toolset=toolset)


async def test_compare_refuses_unequal_containment(
    tmp_path: Path, toolset: WebMCPToolset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The comparison is refused rather than made, when it would be dishonest.

    The mismatch used here is the trust level, because that is the field a
    future second executor would differ in — running the *before* leg under a
    weaker boundary than the *after* leg is exactly the mistake F8-05's
    security clause names.
    """
    before = RunTarget(workspace(tmp_path / "b", identifier="b"), "app")
    weaker = Workspace(
        id="a",
        root=(tmp_path / "a").resolve(),
        trust_level=TrustLevel.HARDWARE_ATTESTED,
        allow_network=False,
    )
    (tmp_path / "a").mkdir(parents=True, exist_ok=True)
    with pytest.raises(BenchmarkError, match="sandboxed identically"):
        await Benchmarker(ScriptedExecutor()).compare(
            before=before, after=RunTarget(weaker, "app"), toolset=toolset
        )


async def test_compare_refuses_different_application_directories(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    before = RunTarget(workspace(tmp_path / "b", identifier="b"), "app")
    after = RunTarget(workspace(tmp_path / "a", identifier="a"), "other")
    with pytest.raises(BenchmarkError, match="different application directories"):
        await Benchmarker(ScriptedExecutor()).compare(before=before, after=after, toolset=toolset)


async def test_a_before_workspace_holding_the_integration_is_refused(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    """The sides must be the workspaces they claim to be.

    The flag comes from the harness configuration's own filesystem check, so a
    caller that mixes the two workspaces up is caught by the trace rather than
    trusted.
    """
    executor = ScriptedExecutor(
        traces={
            "b": trace_body(scenario_trace(registered=True, presence=IntegrationPresence.PRESENT)),
            "a": trace_body(scenario_trace(registered=True, presence=IntegrationPresence.PRESENT)),
        }
    )
    before, after = _targets(tmp_path)
    with pytest.raises(BenchmarkError, match="already contains the generated integration"):
        await Benchmarker(executor).compare(before=before, after=after, toolset=toolset)


async def test_an_after_workspace_without_the_integration_is_refused(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    executor = ScriptedExecutor(
        traces={
            "b": trace_body(scenario_trace(registered=False, presence=IntegrationPresence.ABSENT)),
            "a": trace_body(scenario_trace(registered=False, presence=IntegrationPresence.ABSENT)),
        }
    )
    before, after = _targets(tmp_path)
    with pytest.raises(BenchmarkError, match="measured an untransformed application"):
        await Benchmarker(executor).compare(before=before, after=after, toolset=toolset)


async def test_a_side_whose_trace_never_arrives_measures_nothing(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    """End to end through the runner: absent, with a reason, and no change."""
    executor = ScriptedExecutor(
        traces={
            "a": trace_body(scenario_trace(registered=True, presence=IntegrationPresence.PRESENT))
        },
        exit_code=1,
    )
    before, after = _targets(tmp_path)
    report = await Benchmarker(executor).compare(
        before=before, after=after, toolset=toolset, benchmark_id="bench"
    )

    for row in report.rows:
        assert isinstance(row.before, Absence)
        assert isinstance(row.after, Measurement)
        assert row.delta is None
    assert "wrote no trace" in report.run(RunSide.BEFORE).trace_note
    assert "exited 1" in report.run(RunSide.BEFORE).trace_note


async def test_a_sandbox_refusal_is_an_error_not_an_empty_comparison(
    tmp_path: Path, toolset: WebMCPToolset
) -> None:
    class Refusing(ScriptedExecutor):
        async def run(self, ws: Workspace, cmd: Command) -> CommandResult:
            raise SandboxError("node is not on the allowlist")

    before, after = _targets(tmp_path)
    with pytest.raises(BenchmarkError, match="could not be executed"):
        await Benchmarker(Refusing()).compare(before=before, after=after, toolset=toolset)


def _targets(tmp_path: Path) -> tuple[RunTarget, RunTarget]:
    before = workspace(tmp_path / "b", identifier="b")
    after = workspace(tmp_path / "a", identifier="a")
    for ws in (before, after):
        (ws.root / "app").mkdir(parents=True, exist_ok=True)
    return RunTarget(before, "app"), RunTarget(after, "app")


# ---------------------------------------------------------------------------
# 5. the cross-tier contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["benchmark-report.json", "benchmark-report-partial.json"])
def test_the_web_fixtures_are_reports_this_backend_produces(name: str) -> None:
    """The web tier renders these; the backend has to be able to emit them.

    A hand-written fixture that models the wrong shape and code that agrees
    with it is this phase's recurring failure, so the fixture is validated
    against the model rather than against itself — and the round trip catches a
    field the backend would never emit, which validation alone would ignore.
    """
    payload = json.loads((WEB_FIXTURES / name).read_text())
    report = BenchmarkReport.model_validate(payload)
    assert report.model_dump(mode="json") == payload


def test_the_partial_fixture_is_the_absent_case_and_the_other_is_not() -> None:
    """The two fixtures must actually differ, or the web tests prove nothing."""
    measured = BenchmarkReport.model_validate(
        json.loads((WEB_FIXTURES / "benchmark-report.json").read_text())
    )
    partial = BenchmarkReport.model_validate(
        json.loads((WEB_FIXTURES / "benchmark-report-partial.json").read_text())
    )
    assert not measured.absences
    assert len(measured.measurements) == 2 * len(InteractionMetric)
    assert len(partial.absences) == len(InteractionMetric)
    assert all(cell.side is RunSide.BEFORE for cell in partial.absences)
    # A measured zero exists in the fixture, so the web tier's "absent is not
    # zero" test is distinguishing two states that both occur.
    assert [m for m in measured.measurements if m.value == 0.0]


# ---------------------------------------------------------------------------
# 6. the integration run, against the real fixture
# ---------------------------------------------------------------------------


INTEGRATION_ENV = "MCPFORGE_VALIDATION_INTEGRATION_REQUIRED"

#: Node reserves a large virtual address range per WebAssembly instance; the
#: executor's default `RLIMIT_AS` kills it. Same reasoning as `test_validator`.
BENCHMARK_ADDRESS_SPACE_MB = 256 * 1024


def _skip_or_fail(message: str) -> None:
    if os.environ.get(INTEGRATION_ENV) == "1":
        pytest.fail(f"{INTEGRATION_ENV}=1 and {message}")
    pytest.skip(message)


def _require_toolchain() -> None:
    if not (NODE_MODULES / "vitest" / "vitest.mjs").is_file():
        _skip_or_fail("node_modules/vitest is not installed; run npm ci at the repository root")
    if not Path("/usr/bin/node").exists():
        _skip_or_fail("/usr/bin/node is missing; the sandbox PATH cannot reach the toolchain")
    if not DEMO_APP.is_dir():
        _skip_or_fail("the demo fixture is missing")


def _materialise(app: Path, toolset: WebMCPToolset, *, patched: bool) -> None:
    """The real fixture, and the real generated patch only on the after side."""
    shutil.copytree(
        DEMO_APP,
        app,
        ignore=shutil.ignore_patterns("node_modules", ".next", "tsconfig.tsbuildinfo"),
    )
    subprocess.run(  # noqa: S603
        ["cp", "-al", str(NODE_MODULES), str(app / "node_modules")],  # noqa: S607
        check=True,
    )
    if not patched:
        return
    for change in generate_patch(toolset, base_commit="demo").files:
        destination = app / change.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(change.contents)


async def test_the_scenario_measures_the_real_fixture_before_and_after(
    toolset: WebMCPToolset,
) -> None:
    """The real thing: real fixture, real patch, real sandbox, real trace.

    Nothing is stubbed except the developer's own approval endpoint, which does
    not exist in a workspace with no network — and what that check measures is
    that the gated tool *refused to act*, which happens before the request.
    """
    _require_toolchain()

    executor = DevelopmentSecureExecutor(memory_mb=BENCHMARK_ADDRESS_SPACE_MB, cpu_seconds=1200)
    async with (
        executor.workspace(WorkspaceSpec(run_id="f8-05-before")) as before_ws,
        executor.workspace(WorkspaceSpec(run_id="f8-05-after")) as after_ws,
    ):
        _materialise(before_ws.root / "app", toolset, patched=False)
        _materialise(after_ws.root / "app", toolset, patched=True)
        report = await Benchmarker(executor, timeout_seconds=900).compare(
            before=RunTarget(before_ws, "app"),
            after=RunTarget(after_ws, "app"),
            toolset=toolset,
            benchmark_id="f8-05-integration",
        )

    for run in report.runs:
        assert run.trace_complete, f"{run.side.value}: {run.trace_note}"
        assert run.evidence.exit_code == 0, run.evidence.stderr_excerpt[-2000:]
    assert report.run(RunSide.BEFORE).integration is IntegrationPresence.ABSENT
    assert report.run(RunSide.AFTER).integration is IntegrationPresence.PRESENT

    # Every number is a measurement of this run, and nothing is absent.
    assert not report.absences
    for measurement in report.measurements:
        assert measurement.run_id.startswith("f8-05-integration:")
        assert measurement.recorded_at.tzinfo is not None

    gated = [tool for tool in toolset.tools if tool.approval_required]
    open_tools = [tool for tool in toolset.tools if not tool.approval_required]

    after_cells = measure_run(report.run(RunSide.AFTER))
    before_cells = measure_run(report.run(RunSide.BEFORE))

    # The counts follow the toolset, not a constant: every open tool completes,
    # every gated tool stops at an approval, and neither happens before.
    assert _value(after_cells, InteractionMetric.TASKS_ATTEMPTED) == len(toolset.tools)
    assert _value(after_cells, InteractionMetric.TASKS_COMPLETED) == len(open_tools)
    assert _value(after_cells, InteractionMetric.APPROVAL_POINTS) == len(gated)
    assert _value(after_cells, InteractionMetric.ERRORS) == 0

    assert _value(before_cells, InteractionMetric.TASKS_ATTEMPTED) == len(toolset.tools)
    assert _value(before_cells, InteractionMetric.TASKS_COMPLETED) == 0
    assert _value(before_cells, InteractionMetric.APPROVAL_POINTS) == 0
    # Two attempts per task, each failing to find a tool that is not there.
    assert _value(before_cells, InteractionMetric.ERRORS) == 2 * len(toolset.tools)
    assert _value(before_cells, InteractionMetric.RETRIES) == len(toolset.tools)

    assert _value(before_cells, InteractionMetric.ELAPSED_SECONDS) > 0
    assert _value(after_cells, InteractionMetric.ELAPSED_SECONDS) > 0


async def test_a_broken_handler_moves_the_real_numbers(toolset: WebMCPToolset) -> None:
    """Prove the integration run can go red, by breaking the application.

    The generated handler for the first open tool is rewritten to throw. The
    scenario records the throw, retries, records it again, and the task never
    completes — so `ERRORS` rises and `TASKS_COMPLETED` falls, both out of the
    trace rather than out of any expectation held here.
    """
    _require_toolchain()

    open_tool = next(tool for tool in toolset.tools if not tool.approval_required)
    executor = DevelopmentSecureExecutor(memory_mb=BENCHMARK_ADDRESS_SPACE_MB, cpu_seconds=1200)
    async with (
        executor.workspace(WorkspaceSpec(run_id="f8-05-before-red")) as before_ws,
        executor.workspace(WorkspaceSpec(run_id="f8-05-after-red")) as after_ws,
    ):
        _materialise(before_ws.root / "app", toolset, patched=False)
        app = after_ws.root / "app"
        _materialise(app, toolset, patched=True)

        handler = app / "src" / "webmcp" / "tools" / f"{open_tool.handler_name}.ts"
        source = handler.read_text()
        broken = source.replace(
            "  try {",
            '  throw new Error("broken on purpose by the F8-05 test");\n  try {',
            1,
        )
        assert broken != source, "the handler shape changed; this mutation no longer applies"
        handler.write_text(broken)

        report = await Benchmarker(executor, timeout_seconds=900).compare(
            before=RunTarget(before_ws, "app"),
            after=RunTarget(after_ws, "app"),
            toolset=toolset,
            benchmark_id="f8-05-red",
        )

    after_cells = measure_run(report.run(RunSide.AFTER))
    open_tools = [tool for tool in toolset.tools if not tool.approval_required]
    assert _value(after_cells, InteractionMetric.TASKS_COMPLETED) == len(open_tools) - 1
    # One throw per attempt, and the scenario makes two attempts.
    assert _value(after_cells, InteractionMetric.ERRORS) == 2
    assert _value(after_cells, InteractionMetric.RETRIES) == 1


def _value(
    cells: dict[InteractionMetric, Measurement | Absence], metric: InteractionMetric
) -> float:
    cell = cells[metric]
    assert isinstance(cell, Measurement), f"{metric.value} was not measured"
    return cell.value


def test_the_integration_env_name_is_the_one_ci_sets() -> None:
    """The same switch F8-04's suite uses, so one variable arms both."""
    assert INTEGRATION_ENV == "MCPFORGE_VALIDATION_INTEGRATION_REQUIRED"
