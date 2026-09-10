"""Before/after demonstration — ticket F8-05, `04_FRONTEND_SPEC.md` §9.

The product's central claim is that the transformation makes an application
usable by an agent. This module is the measurement behind that claim, and it is
built so that the claim cannot be made without the measurement.

**What is measured.** One scripted scenario — attempt every approved tool as a
task, through the browser model-context API, retrying a failed attempt once —
executed twice: once against the application *before* the generated integration
is applied, once *after*. The scenario writes a JSONL trace of what it actually
did; the six metrics of the ticket are counted out of that trace.

**What is not measured, stated plainly.** This measures agent interaction
*through WebMCP*. It does not measure an agent driving the DOM, reading the
page, or guessing at forms, because MCPForge cannot execute that without a
browser and application-specific expectations — the same limit that leaves
F8-04's `UI_SYNCHRONIZATION` component with no evidence. So the honest reading
of a *before* column is "an agent working through the model context found
nothing to work with", not "an agent could not use this application at all".
`SCENARIO_BOUND` carries that sentence to the UI so the screen states it too.

**Three rules, each with the test that fails if it is violated.**

1. **Every number is a `Measurement`, and a `Measurement` is built in exactly
   one function.** `_measure` is the only constructor site, pinned by
   `test_measurements_are_produced_in_exactly_one_function`, in the same shape
   as F8-04's `CheckEvidence` sweep and with the same stated bound: it matches
   bare and attribute-qualified calls in the AST, and no name-based check
   defeats deliberate indirection.
2. **A metric that was not measured has no value to display.** `Absence` has no
   `value` field at all, so "absent" cannot decay into `0` through a default —
   there is nothing to default. Pinned by
   `test_an_absence_has_no_value_field_to_default`.
3. **Every displayed number traces to a run id and a timestamp.**
   `Measurement.run_id` must name one of the report's two runs, on the correct
   side; `BenchmarkReport` refuses to be constructed otherwise. Pinned by
   `test_a_measurement_cannot_cite_a_run_that_is_not_in_the_report`.

**Security — F8-05's binding requirement.** The before run must be sandboxed
identically to the after run, or the comparison is measuring the sandbox rather
than the transformation. That is enforced twice: `Benchmarker.compare` refuses
to run when the two workspaces differ, and `BenchmarkReport` refuses to exist
when its two runs carry different `SandboxProfile`s. Both sides also run with
no outbound network, like validation, and a networked workspace is refused
outright.

There is no model call in this module. No prompt, no provider, no import that
reaches Gemini — the numbers come from a trace a real process wrote.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mcpforge.agents.validator import capture_helper
from mcpforge.execution.provider import (
    Command,
    CommandResult,
    SandboxError,
    SecureExecutionProvider,
    TrustLevel,
    Workspace,
    resolve_inside,
)
from mcpforge.generation.escaping import as_ts_string
from mcpforge.generation.nextjs import REGISTER_PATH
from mcpforge.generation.test_template import valid_arguments
from mcpforge.logging import get_logger
from mcpforge.models.webmcp import WebMCPToolset
from mcpforge.orchestration.scoring import CheckEvidence

log = get_logger(__name__)

#: The bound on what this benchmark demonstrates, in the words the UI shows.
#: One string, because a caveat that lives only in a docstring is a caveat the
#: developer never reads.
SCENARIO_BOUND: Final = (
    "Both runs execute the same scripted scenario: attempt every approved tool "
    "as a task through the browser model-context API, retrying a failed attempt "
    "once. This measures agent interaction through WebMCP only. It does not "
    "measure an agent driving the page's DOM, which MCPForge cannot execute "
    "without a browser."
)

DEFAULT_BENCHMARK_TIMEOUT_SECONDS: Final = 300

#: How many attempts the scenario makes per task. Two, so that a retry is a
#: thing that can actually be observed: with one attempt `RETRIES` could only
#: ever be zero, and a metric that can only be zero is not a measurement.
MAX_ATTEMPTS_PER_TASK: Final = 2

#: Where the scenario lives inside the workspace, relative to the application
#: root. Under `src/` so the `@/` alias applies, and named so it is obviously
#: not the developer's code.
HARNESS_DIR: Final = "src/webmcp/__benchmark__"
HARNESS_CONFIG: Final = "mcpforge.benchmark.config.mts"
SCENARIO_PATH: Final = f"{HARNESS_DIR}/scenario.test.mts"
ABSENT_REGISTER_PATH: Final = f"{HARNESS_DIR}/absent-register.mts"
TRACE_PATH: Final = "mcpforge.benchmark.trace.jsonl"

#: Same reasoning as the validator: `node_modules/.bin` is deliberately off the
#: sandbox PATH, so local CLIs are run through `node`.
VITEST_CLI: Final = "node_modules/vitest/vitest.mjs"


class BenchmarkError(Exception):
    """The comparison could not be made honestly, so it is not made at all."""


class TraceError(Exception):
    """The recorded trace could not be read as a record of what happened."""


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class RunSide(StrEnum):
    """The two runs. There are exactly two, and they are not interchangeable."""

    BEFORE = "BEFORE"
    AFTER = "AFTER"


class MeasurementUnit(StrEnum):
    COUNT = "COUNT"
    SECONDS = "SECONDS"


class InteractionMetric(StrEnum):
    """The six measurements F8-05 names, and nothing else.

    Row order on screen is this declaration order.
    """

    INTERACTION_STEPS = "INTERACTION_STEPS"
    TASKS_ATTEMPTED = "TASKS_ATTEMPTED"
    TASKS_COMPLETED = "TASKS_COMPLETED"
    ERRORS = "ERRORS"
    RETRIES = "RETRIES"
    APPROVAL_POINTS = "APPROVAL_POINTS"
    ELAPSED_SECONDS = "ELAPSED_SECONDS"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @property
    def unit(self) -> MeasurementUnit:
        return (
            MeasurementUnit.SECONDS
            if self is InteractionMetric.ELAPSED_SECONDS
            else MeasurementUnit.COUNT
        )


_LABELS: Final[dict[InteractionMetric, str]] = {
    InteractionMetric.INTERACTION_STEPS: "Interaction steps",
    InteractionMetric.TASKS_ATTEMPTED: "Tasks attempted",
    InteractionMetric.TASKS_COMPLETED: "Tasks completed",
    InteractionMetric.ERRORS: "Errors",
    InteractionMetric.RETRIES: "Retries",
    InteractionMetric.APPROVAL_POINTS: "Approval points",
    InteractionMetric.ELAPSED_SECONDS: "Elapsed time",
}


class EventKind(StrEnum):
    """Every event the scenario is allowed to emit.

    A trace line naming anything else is a trace this module did not produce,
    and it is rejected rather than ignored — an unknown kind silently dropped
    is a count that is quietly wrong.
    """

    SCENARIO_STARTED = "scenario_started"
    SCENARIO_FINISHED = "scenario_finished"
    TASK_ATTEMPTED = "task_attempted"
    TASK_COMPLETED = "task_completed"
    STEP = "step"
    RETRY = "retry"
    ERROR = "error"
    APPROVAL_REQUIRED = "approval_required"


class IntegrationPresence(StrEnum):
    """Whether the generated integration was in the workspace the run used.

    Observed by the harness configuration with a filesystem check, not assumed
    from which side the caller said it was running. It is what makes "before"
    and "after" claims about the workspace rather than about our bookkeeping.
    """

    PRESENT = "present"
    ABSENT = "absent"


class InteractionEvent(BaseModel):
    """One line of the recorded trace."""

    model_config = ConfigDict(frozen=True)

    kind: EventKind
    #: Epoch milliseconds, from `performance.timeOrigin + performance.now()`,
    #: so the elapsed span has sub-millisecond resolution.
    at_ms: float
    task: str | None = None
    detail: str = Field(default="", max_length=600)
    #: Only ever set on `scenario_started`.
    integration: IntegrationPresence | None = None


# ---------------------------------------------------------------------------
# The sandbox, which must be the same on both sides
# ---------------------------------------------------------------------------


class SandboxProfile(BaseModel):
    """Everything about how a run was contained, in one comparable value.

    F8-05's security requirement is a statement about equality, so it is
    modelled as a value that can be compared rather than as a checklist that
    can be half-applied.
    """

    model_config = ConfigDict(frozen=True)

    trust_level: TrustLevel
    allow_network: bool
    argv: tuple[str, ...]
    cwd: str | None
    timeout_seconds: int
    #: Sorted, so two equal environments compare equal regardless of build order.
    env: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, workspace: Workspace, command: Command) -> SandboxProfile:
        return SandboxProfile(
            trust_level=workspace.trust_level,
            allow_network=workspace.allow_network,
            argv=tuple(command.argv),
            cwd=command.cwd,
            timeout_seconds=command.timeout_seconds,
            env=tuple(sorted(command.env.items())),
        )


# ---------------------------------------------------------------------------
# A measurement, and the absence of one
# ---------------------------------------------------------------------------


class Measurement(BaseModel):
    """One number that may be displayed, with everything needed to trace it.

    `run_id` and `recorded_at` are required and have no defaults, because the
    acceptance criterion is that a displayed number traces to a recorded
    measurement with a timestamp and a run id. A field with a default is a
    field a caller can forget.
    """

    model_config = ConfigDict(frozen=True)

    state: Literal["measured"] = "measured"
    metric: InteractionMetric
    side: RunSide
    value: float
    unit: MeasurementUnit
    run_id: str = Field(min_length=1, max_length=120)
    recorded_at: datetime
    #: How the number was obtained, in one sentence, for the evidence popover.
    derivation: str = Field(min_length=1, max_length=300)
    #: The command whose execution produced the trace this was counted from.
    evidence: CheckEvidence


class Absence(BaseModel):
    """A metric that was not measured.

    **There is deliberately no `value` field.** "Absent rather than defaulted"
    is not a rendering convention here; it is the shape of the record. Nothing
    downstream can read a zero off this, because there is no number on it to
    read.
    """

    model_config = ConfigDict(frozen=True)

    state: Literal["not_measured"] = "not_measured"
    metric: InteractionMetric
    side: RunSide
    reason: str = Field(min_length=1, max_length=400)


MetricCell = Annotated[Measurement | Absence, Field(discriminator="state")]


class MetricRow(BaseModel):
    """One row of the comparison: the same metric, both sides, and the change."""

    model_config = ConfigDict(frozen=True)

    metric: InteractionMetric
    label: str
    unit: MeasurementUnit
    before: MetricCell
    after: MetricCell
    #: `after - before`, and **only** when both sides were measured. Never an
    #: estimate, never a comparison against a defaulted zero.
    delta: float | None = None

    @model_validator(mode="after")
    def _delta_is_arithmetic_over_two_measurements(self) -> MetricRow:
        if self.before.metric is not self.metric or self.after.metric is not self.metric:
            raise ValueError(f"a cell in the {self.metric.value} row is for another metric")
        if self.before.side is not RunSide.BEFORE or self.after.side is not RunSide.AFTER:
            raise ValueError(f"the {self.metric.value} row has a cell on the wrong side")
        if self.label != self.metric.label or self.unit is not self.metric.unit:
            raise ValueError(f"the {self.metric.value} row is labelled as something else")

        left = self.before if isinstance(self.before, Measurement) else None
        right = self.after if isinstance(self.after, Measurement) else None
        if left is None or right is None:
            if self.delta is not None:
                raise ValueError(
                    f"{self.metric.value} has a change but was not measured on both sides"
                )
            return self
        if self.delta is None:
            raise ValueError(f"{self.metric.value} was measured on both sides but has no change")
        if not math.isclose(self.delta, right.value - left.value, abs_tol=1e-9):
            raise ValueError(f"{self.metric.value}'s change is not the difference of its two sides")
        return self


# ---------------------------------------------------------------------------
# A run and a report
# ---------------------------------------------------------------------------


class BenchmarkRun(BaseModel):
    """One side's execution: what ran, when, under what containment."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1, max_length=120)
    side: RunSide
    workspace_id: str = Field(min_length=1, max_length=200)
    started_at: datetime
    completed_at: datetime
    sandbox: SandboxProfile
    evidence: CheckEvidence
    events: tuple[InteractionEvent, ...]
    #: Whether the trace is a complete record of a scenario that ran to the end.
    #: Counting events out of a truncated trace produces numbers that look like
    #: measurements and are not, so an incomplete trace measures nothing.
    trace_complete: bool
    #: Empty when the trace is complete; otherwise why it is not usable.
    trace_note: str = ""
    integration: IntegrationPresence | None = None

    @model_validator(mode="after")
    def _an_incomplete_trace_says_why(self) -> BenchmarkRun:
        if self.trace_complete and self.trace_note:
            raise ValueError("a complete trace carries no note explaining its absence")
        if not self.trace_complete and not self.trace_note:
            raise ValueError("an unusable trace must say why it is unusable")
        return self


class BenchmarkReport(BaseModel):
    """The before/after comparison. Cannot be constructed dishonestly.

    Four invariants are enforced here rather than trusted to the producer,
    because this is the object the API serialises and the screen renders:

    1. Exactly one run per side.
    2. Both runs carry the report's single `SandboxProfile` — F8-05's security
       requirement, as a construction-time refusal.
    3. Neither run had a network.
    4. Every metric appears exactly once, in enum order, and every measured
       cell cites the run id of its own side.
    """

    model_config = ConfigDict(frozen=True)

    benchmark_id: str = Field(min_length=1, max_length=120)
    sandbox: SandboxProfile
    runs: tuple[BenchmarkRun, ...]
    rows: tuple[MetricRow, ...]
    scenario_bound: str = SCENARIO_BOUND

    @model_validator(mode="after")
    def _check(self) -> BenchmarkReport:
        sides = [run.side for run in self.runs]
        if sorted(sides) != sorted(RunSide):
            raise ValueError("a report has exactly one run per side")

        for run in self.runs:
            if run.sandbox != self.sandbox:
                raise ValueError(
                    f"the {run.side.value} run was sandboxed differently from the report's "
                    "profile; the comparison would be measuring the sandbox"
                )
        if self.sandbox.allow_network:
            raise ValueError(
                "a benchmark run must use a workspace with no outbound network, "
                "and both sides must use the same one (05_FEATURE_TICKETS.md F8-05)"
            )

        by_side = {run.side: run for run in self.runs}
        if tuple(row.metric for row in self.rows) != tuple(InteractionMetric):
            raise ValueError("a report carries every metric exactly once, in order")

        for row in self.rows:
            for cell in (row.before, row.after):
                if isinstance(cell, Measurement) and cell.run_id != by_side[cell.side].run_id:
                    raise ValueError(
                        f"{row.metric.value} on the {cell.side.value} side cites run "
                        f"{cell.run_id!r}, which is not that side's run"
                    )
        return self

    def run(self, side: RunSide) -> BenchmarkRun:
        for candidate in self.runs:
            if candidate.side is side:
                return candidate
        raise KeyError(side)

    def row(self, metric: InteractionMetric) -> MetricRow:
        for row in self.rows:
            if row.metric is metric:
                return row
        raise KeyError(metric)

    @property
    def measurements(self) -> tuple[Measurement, ...]:
        return tuple(
            cell
            for row in self.rows
            for cell in (row.before, row.after)
            if isinstance(cell, Measurement)
        )

    @property
    def absences(self) -> tuple[Absence, ...]:
        return tuple(
            cell
            for row in self.rows
            for cell in (row.before, row.after)
            if isinstance(cell, Absence)
        )


# ---------------------------------------------------------------------------
# Reading a trace
# ---------------------------------------------------------------------------


def parse_trace(text: str) -> tuple[InteractionEvent, ...]:
    """Parse the JSONL trace. Strict: a line we cannot read is an error.

    Skipping an unreadable line would produce a count that is silently short —
    a number that looks measured and is not. `TraceError` instead, which the
    caller turns into an absence with a reason.
    """
    events: list[InteractionEvent] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceError(f"line {number} of the trace is not JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise TraceError(f"line {number} of the trace is not an object")
        try:
            events.append(InteractionEvent.model_validate(payload))
        except ValueError as exc:
            raise TraceError(f"line {number} of the trace is not an event: {exc}") from exc
    return tuple(events)


@dataclass(frozen=True)
class TraceReading:
    """What a trace turned out to be. Complete, or absent with a reason."""

    events: tuple[InteractionEvent, ...]
    complete: bool
    note: str
    integration: IntegrationPresence | None


def read_trace(workspace: Workspace, *, app_dir: str = ".") -> TraceReading:
    """Read and validate the trace the scenario wrote into the workspace.

    Every path goes through `resolve_inside`, the single implementation of the
    workspace path jail (`execution/provider.py`).
    """
    try:
        path: Path = resolve_inside(workspace, f"{app_dir}/{TRACE_PATH}")
    except SandboxError as exc:
        return TraceReading((), False, f"the trace path is not inside the workspace: {exc}", None)

    if not path.is_file():
        return TraceReading(
            (),
            False,
            "the scenario wrote no trace, so this run measured nothing.",
            None,
        )

    try:
        events = parse_trace(path.read_text(encoding="utf-8", errors="replace"))
    except TraceError as exc:
        return TraceReading((), False, f"the recorded trace could not be read: {exc}", None)

    started = [e for e in events if e.kind is EventKind.SCENARIO_STARTED]
    finished = [e for e in events if e.kind is EventKind.SCENARIO_FINISHED]
    if len(started) != 1 or len(finished) != 1:
        return TraceReading(
            events,
            False,
            (
                "the scenario did not run to completion, so its counts would be partial "
                "rather than measured."
            ),
            started[0].integration if len(started) == 1 else None,
        )
    if finished[0].at_ms < started[0].at_ms:
        return TraceReading(
            events, False, "the trace's end precedes its start; it is not a usable record.", None
        )

    return TraceReading(events, True, "", started[0].integration)


# ---------------------------------------------------------------------------
# Turning a trace into measurements
# ---------------------------------------------------------------------------

#: The one place a metric is tied to the event that counts it. `ELAPSED_SECONDS`
#: is absent because it is a span, not a count, and is derived below.
_COUNTED_EVENT: Final[dict[InteractionMetric, EventKind]] = {
    InteractionMetric.INTERACTION_STEPS: EventKind.STEP,
    InteractionMetric.TASKS_ATTEMPTED: EventKind.TASK_ATTEMPTED,
    InteractionMetric.TASKS_COMPLETED: EventKind.TASK_COMPLETED,
    InteractionMetric.ERRORS: EventKind.ERROR,
    InteractionMetric.RETRIES: EventKind.RETRY,
    InteractionMetric.APPROVAL_POINTS: EventKind.APPROVAL_REQUIRED,
}


def _measure(
    *,
    metric: InteractionMetric,
    run: BenchmarkRun,
    value: float,
    recorded_at: datetime,
    derivation: str,
) -> Measurement:
    """The only place a `Measurement` is constructed.

    It takes the run, so `run_id`, `side` and `evidence` cannot be supplied
    independently of the execution they claim to describe.
    `test_measurements_are_produced_in_exactly_one_function` sweeps the backend
    for a second call site. **Stated bound**, matching F8-04's sweeps: it
    matches bare and attribute-qualified calls named `Measurement` in the AST.
    A dict coerced by pydantic into this position has no call site to find, so
    `test_no_module_assembles_a_metric_row_from_a_mapping` sweeps for that
    separately — the pair is the guarantee, not either half.

    The class is named explicitly rather than through `cls`, so the sweep has a
    name to match.
    """
    return Measurement(
        metric=metric,
        side=run.side,
        value=value,
        unit=metric.unit,
        run_id=run.run_id,
        recorded_at=recorded_at,
        derivation=derivation,
        evidence=run.evidence,
    )


def measure_run(
    run: BenchmarkRun, *, recorded_at: datetime | None = None
) -> dict[InteractionMetric, Measurement | Absence]:
    """Every metric for one run: a measurement, or an absence with a reason.

    **The bound worth stating, because the shape of the data suggests more than
    the producer does.** The record is per `(metric, side)` because that is how
    the report and the screen address a number. Today the producer decides at
    run granularity: a run whose trace is complete measures all seven metrics,
    and a run whose trace is missing, unreadable or truncated measures none of
    them. There is no metric the current scenario measures selectively, and
    this docstring does not claim one.
    """
    stamp = recorded_at or datetime.now(UTC)
    cells: dict[InteractionMetric, Measurement | Absence] = {}

    if not run.trace_complete:
        for metric in InteractionMetric:
            cells[metric] = Absence(metric=metric, side=run.side, reason=run.trace_note)
        return cells

    for metric, kind in _COUNTED_EVENT.items():
        cells[metric] = _measure(
            metric=metric,
            run=run,
            value=float(sum(1 for event in run.events if event.kind is kind)),
            recorded_at=stamp,
            derivation=f"Count of {kind.value!r} events in the trace recorded by run {run.run_id}.",
        )

    started = next(e for e in run.events if e.kind is EventKind.SCENARIO_STARTED)
    finished = next(e for e in run.events if e.kind is EventKind.SCENARIO_FINISHED)
    cells[InteractionMetric.ELAPSED_SECONDS] = _measure(
        metric=InteractionMetric.ELAPSED_SECONDS,
        run=run,
        value=(finished.at_ms - started.at_ms) / 1000.0,
        recorded_at=stamp,
        derivation=(
            "Span between the scenario's first and last recorded event, from the "
            "process clock inside the sandbox."
        ),
    )
    return cells


def build_rows(
    before: BenchmarkRun, after: BenchmarkRun, *, recorded_at: datetime | None = None
) -> tuple[MetricRow, ...]:
    """Assemble the comparison. Every row, in enum order, both sides."""
    if before.side is not RunSide.BEFORE or after.side is not RunSide.AFTER:
        raise BenchmarkError("build_rows takes the before run and then the after run")

    stamp = recorded_at or datetime.now(UTC)
    before_cells = measure_run(before, recorded_at=stamp)
    after_cells = measure_run(after, recorded_at=stamp)

    rows: list[MetricRow] = []
    for metric in InteractionMetric:
        left = before_cells[metric]
        right = after_cells[metric]
        delta = (
            right.value - left.value
            if isinstance(left, Measurement) and isinstance(right, Measurement)
            else None
        )
        rows.append(
            MetricRow(
                metric=metric,
                label=metric.label,
                unit=metric.unit,
                before=left,
                after=right,
                delta=delta,
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# The harness, written into the workspace only
# ---------------------------------------------------------------------------


def _config_file() -> str:
    """The vitest configuration for the scenario.

    It performs the one observation that makes a side mean something: whether
    the generated `register.ts` is actually in this workspace. The scenario
    imports `@/webmcp/register` either way; this file points that specifier at
    the real integration when it exists and at a stub that registers nothing
    when it does not. Two consequences worth being explicit about:

    - the scenario source is byte-identical on both sides, so nothing about the
      *before* run is a different experiment; and
    - the presence flag reaching the trace is a filesystem fact, not the
      caller's assertion about which side it thinks it is running.
    """
    return f"""// MCPForge benchmark harness — written into the ephemeral benchmark workspace
// only. It is not part of the generated patch and never reaches your repository.
import {{ existsSync }} from "node:fs";
import {{ fileURLToPath }} from "node:url";
import {{ defineConfig }} from "vitest/config";

const root = fileURLToPath(new URL(".", import.meta.url));
const generated = root + {as_ts_string(REGISTER_PATH)};
const integrationPresent = existsSync(generated);

export default defineConfig({{
  root,
  resolve: {{
    alias: [
      // Order matters: the specific rule must precede the generic `@/` one.
      {{
        find: /^@\\/webmcp\\/register$/,
        replacement: integrationPresent ? generated : root + {as_ts_string(ABSENT_REGISTER_PATH)},
      }},
      {{ find: /^@\\//, replacement: root + "src/" }},
    ],
  }},
  test: {{
    environment: "node",
    include: [{as_ts_string(SCENARIO_PATH)}],
    watch: false,
    passWithNoTests: false,
    env: {{
      MCPFORGE_BENCHMARK_INTEGRATION: integrationPresent
        ? {as_ts_string(IntegrationPresence.PRESENT.value)}
        : {as_ts_string(IntegrationPresence.ABSENT.value)},
    }},
  }},
}});
"""


def _absent_register_file() -> str:
    """What an application looks like to a WebMCP agent before transformation.

    It is not a mock of the integration. It is the honest stand-in for its
    absence: a `registerWebMCPTools` that registers nothing, so the scenario
    discovers no tools — which is exactly what an agent finds on an application
    that has never been transformed.
    """
    return """// MCPForge benchmark harness — the *before* state, in one function.
//
// This is not a mock of the generated integration. The application under
// measurement has no `src/webmcp/register.ts`, so an agent asking the browser's
// model context for this application's tools is offered none. Registering
// nothing is that fact, expressed as the module the scenario imports.
export function registerWebMCPTools(): () => void {
  return () => {};
}
"""


def _scenario_file(toolset: WebMCPToolset) -> str:
    """The scenario, identical on both sides.

    It asserts nothing. A vitest assertion failure would make the exit code the
    verdict, and this file is not a check — it is an instrument. What it
    produces is the trace; what a failed task looks like is an `error` event,
    counted like any other.
    """
    tasks = "\n".join(
        f"  {{ name: {as_ts_string(tool.name)}, args: {{ {valid_arguments(tool)} }} }},"
        for tool in toolset.tools
    )
    return f"""// MCPForge benchmark scenario — F8-05.
//
// Written into the ephemeral benchmark workspace for one run. It is never part
// of the generated patch and never reaches your repository.
//
// The same file runs before and after the transformation. It records what it
// did to {TRACE_PATH} and asserts nothing, because it is measuring rather than
// checking.
import {{ appendFileSync, writeFileSync }} from "node:fs";
import {{ fileURLToPath }} from "node:url";

import {{ describe, it, vi }} from "vitest";

import {{ registerWebMCPTools }} from "@/webmcp/register";

{capture_helper()}
const TRACE = fileURLToPath(new URL("../../../{TRACE_PATH}", import.meta.url));
const MAX_ATTEMPTS = {MAX_ATTEMPTS_PER_TASK};

interface Task {{
  name: string;
  args: Record<string, unknown>;
}}

const TASKS: Task[] = [
{tasks}
];

function emit(kind: string, task: string | null, detail: string, integration?: string): void {{
  const line = {{
    kind,
    // Epoch milliseconds with sub-millisecond resolution, so the elapsed span
    // is the scenario's own clock rather than the process launch overhead.
    at_ms: performance.timeOrigin + performance.now(),
    task,
    detail: detail.slice(0, 600),
    ...(integration === undefined ? {{}} : {{ integration }}),
  }};
  appendFileSync(TRACE, JSON.stringify(line) + "\\n");
}}

describe("mcpforge benchmark scenario", () => {{
  it("attempts every approved tool as a task", async () => {{
    writeFileSync(TRACE, "");
    // The generated gate posts to an approval endpoint the developer owns.
    // There is no server in this workspace and no network, so the request is
    // stubbed. What is measured is that the tool refused to act and demanded
    // an approval — not that an approval service answered.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({{ approvalId: "mcpforge-benchmark" }})),
    );

    emit(
      "scenario_started",
      null,
      "the agent begins with the application's own model context",
      process.env.MCPFORGE_BENCHMARK_INTEGRATION ?? "absent",
    );

    let tools: Record<string, CapturedTool | undefined> = {{}};
    try {{
      tools = await registerAndCapture();
    }} catch (error) {{
      emit("error", null, `tool discovery failed: ${{String(error)}}`);
    }}

    for (const task of TASKS) {{
      emit("task_attempted", task.name, "the agent takes on this task");
      for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {{
        if (attempt > 1) {{
          emit("retry", task.name, `attempt ${{attempt}} of ${{MAX_ATTEMPTS}}`);
        }}
        emit("step", task.name, "look the tool up in the model context and call it");

        const tool = tools[task.name];
        if (tool === undefined) {{
          emit("error", task.name, "no tool with this name is registered");
          continue;
        }}

        let result: unknown;
        try {{
          result = await tool.execute(task.args);
        }} catch (error) {{
          emit("error", task.name, `the call threw: ${{String(error)}}`);
          continue;
        }}

        const shape = (result ?? {{}}) as {{ ok?: unknown; awaitingApproval?: unknown }};
        if (shape.awaitingApproval === true) {{
          emit("approval_required", task.name, "the tool stopped and asked for approval");
          break;
        }}
        if (shape.ok === true) {{
          emit("task_completed", task.name, "the tool returned a result");
          break;
        }}
        emit("error", task.name, `the tool returned ${{JSON.stringify(result)}}`);
      }}
    }}

    emit("scenario_finished", null, "the agent has no further tasks");
    vi.unstubAllGlobals();
  }});
}});
"""


def harness_files(toolset: WebMCPToolset) -> dict[str, str]:
    """Every harness file, keyed by its path relative to the application root.

    The same three files on both sides. Nothing here varies with `RunSide`,
    which is what makes the two runs the same experiment.
    """
    return {
        HARNESS_CONFIG: _config_file(),
        ABSENT_REGISTER_PATH: _absent_register_file(),
        SCENARIO_PATH: _scenario_file(toolset),
    }


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunTarget:
    """One side's workspace and the directory the application sits in."""

    workspace: Workspace
    app_dir: str = "."


class Benchmarker:
    """Runs the scenario twice and compares. Deterministic, holds no provider.

    Constructed with a `SecureExecutionProvider` and nothing else, so there is
    no parameter through which a model could be supplied. Both sides go through
    the *same* executor instance, which is what makes the CPU, memory,
    wall-clock and output limits identical as well as the workspace flags.
    """

    name = "benchmark"
    step = "Measuring agent interaction before and after"

    def __init__(
        self,
        executor: SecureExecutionProvider,
        *,
        timeout_seconds: int = DEFAULT_BENCHMARK_TIMEOUT_SECONDS,
    ) -> None:
        self._executor = executor
        self._timeout_seconds = timeout_seconds

    def scenario_command(self, *, app_dir: str = ".") -> Command:
        """The command, built once and used for both sides.

        One function, so the two runs cannot receive different arguments,
        different timeouts or a different environment.
        """
        return Command(
            argv=("node", VITEST_CLI, "run", "--config", HARNESS_CONFIG, SCENARIO_PATH),
            cwd=app_dir,
            timeout_seconds=self._timeout_seconds,
            env={
                "NO_COLOR": "1",
                "NPM_CONFIG_UPDATE_NOTIFIER": "false",
                "NEXT_TELEMETRY_DISABLED": "1",
            },
        )

    def write_harness(
        self, workspace: Workspace, toolset: WebMCPToolset, *, app_dir: str = "."
    ) -> list[str]:
        """Write the harness into a workspace. Returns the paths written.

        Every write goes through `resolve_inside`, so a harness path can never
        land outside the jail.
        """
        written: list[str] = []
        for relative, contents in harness_files(toolset).items():
            target: Path = resolve_inside(workspace, f"{app_dir}/{relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
            written.append(relative)
        return sorted(written)

    async def run_side(
        self,
        side: RunSide,
        target: RunTarget,
        toolset: WebMCPToolset,
        *,
        run_id: str,
    ) -> BenchmarkRun:
        """Execute the scenario once and record what happened."""
        if target.workspace.allow_network:
            raise BenchmarkError(
                "a benchmark run must use a workspace with no outbound network "
                "(05_FEATURE_TICKETS.md F8-05, 03_SECURITY_ACCESS.md §3)"
            )

        command = self.scenario_command(app_dir=target.app_dir)
        self.write_harness(target.workspace, toolset, app_dir=target.app_dir)

        started_at = datetime.now(UTC)
        try:
            result: CommandResult = await self._executor.run(target.workspace, command)
        except SandboxError as exc:
            raise BenchmarkError(f"the {side.value} run could not be executed: {exc}") from exc
        completed_at = datetime.now(UTC)

        reading = read_trace(target.workspace, app_dir=target.app_dir)
        note = reading.note
        if not reading.complete and not result.ok:
            note = f"{note} The scenario exited {result.exit_code}."

        run = BenchmarkRun(
            run_id=run_id,
            side=side,
            workspace_id=target.workspace.id,
            started_at=started_at,
            completed_at=completed_at,
            sandbox=SandboxProfile.of(target.workspace, command),
            evidence=CheckEvidence.from_result(f"benchmark:{side.value.lower()}", result),
            events=reading.events,
            trace_complete=reading.complete,
            trace_note=note,
            integration=reading.integration,
        )
        log.info(
            "benchmark.run",
            run_id=run_id,
            side=side.value,
            exit_code=result.exit_code,
            trace_complete=reading.complete,
            integration=reading.integration.value if reading.integration else None,
        )
        return run

    async def compare(
        self,
        *,
        before: RunTarget,
        after: RunTarget,
        toolset: WebMCPToolset,
        benchmark_id: str | None = None,
    ) -> BenchmarkReport:
        """Run both sides under identical containment and build the report.

        The security requirement is checked before either side runs — a
        mismatch is refused rather than measured — and again by
        `BenchmarkReport`'s own validator once both profiles exist.
        """
        identifier = benchmark_id or uuid.uuid4().hex
        _refuse_unequal_containment(before, after, self.scenario_command(app_dir=before.app_dir))

        before_run = await self.run_side(
            RunSide.BEFORE, before, toolset, run_id=f"{identifier}:before"
        )
        after_run = await self.run_side(RunSide.AFTER, after, toolset, run_id=f"{identifier}:after")
        _refuse_mislabelled_sides(before_run, after_run)

        report = BenchmarkReport(
            benchmark_id=identifier,
            sandbox=before_run.sandbox,
            runs=(before_run, after_run),
            rows=build_rows(before_run, after_run),
        )
        log.info(
            "benchmark.completed",
            benchmark_id=identifier,
            measured=len(report.measurements),
            absent=len(report.absences),
        )
        return report


def _refuse_unequal_containment(before: RunTarget, after: RunTarget, command: Command) -> None:
    """F8-05's security requirement, checked before anything runs."""
    left = SandboxProfile.of(before.workspace, command)
    right = SandboxProfile.of(after.workspace, command)
    if left.allow_network or right.allow_network:
        raise BenchmarkError(
            "a benchmark run must use a workspace with no outbound network "
            "(05_FEATURE_TICKETS.md F8-05, 03_SECURITY_ACCESS.md §3)"
        )
    if before.app_dir != after.app_dir:
        raise BenchmarkError(
            "the two runs use different application directories, so the command "
            "would not be the same on both sides"
        )
    if left != right:
        raise BenchmarkError(
            "the before run would not be sandboxed identically to the after run, "
            "so the comparison would be measuring the sandbox rather than the "
            "transformation"
        )


def _refuse_mislabelled_sides(before: BenchmarkRun, after: BenchmarkRun) -> None:
    """Each side must be the workspace it claims to be.

    The presence flag is the harness configuration's own filesystem check, so
    this catches the two ways a comparison becomes a lie: an *after* workspace
    that never had the patch applied, and a *before* workspace that did.

    Only enforced when the flag is known. A run with no usable trace has no
    flag, and it measures nothing anyway — inventing a verdict about it would
    be the same defect this module exists to avoid.
    """
    if before.integration is IntegrationPresence.PRESENT:
        raise BenchmarkError(
            "the before workspace already contains the generated integration; "
            "there is nothing to compare"
        )
    if after.integration is IntegrationPresence.ABSENT:
        raise BenchmarkError(
            "the after workspace does not contain the generated integration; "
            "the run measured an untransformed application"
        )
