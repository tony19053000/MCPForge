"""Agent Readiness Score — ticket F8-04, 02_ARCHITECTURE.md §11.

The score is arithmetic over commands that were actually executed. There is no
model call in this module, no prompt, and no default: a component for which no
check produced evidence scores **zero**, and says so.

Three properties, each named with the test that fails if it is violated:

1. **Every point traces to an exit code.** `ComponentScore` is constructed in
   exactly one function, `score_components`, and its inputs are `CheckEvidence`
   records built in exactly one place, `CheckEvidence.from_result`, out of a
   `CommandResult` returned by the secure executor. Pinned by
   `test_component_scores_are_produced_in_exactly_one_function` and
   `test_check_evidence_is_constructed_in_exactly_one_place`.
2. **No scoring prompt exists.** No module that reaches Gemini may name any
   scoring symbol, and no module that names one may reach Gemini. Pinned by
   `test_no_module_both_prompts_gemini_and_touches_the_score`, an AST sweep with
   a non-empty self-guard, in the same shape as F8-01's single-producer sweep.
   Its limit is the same and is stated there: it matches on names.
3. **The weights are §11's weights.** Not a copy of them: the table in
   `02_ARCHITECTURE.md` §11 is parsed and compared row by row, by
   `test_the_weights_are_exactly_the_ones_in_the_architecture_document`.

**Rounding is downward, deliberately.** Three of four passing checks in a
25-point component is 18, not 19: a point that was not earned is not awarded.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from mcpforge.execution.provider import CommandResult

#: How much of a command's output is kept as evidence. Enough to explain a
#: failure in the UI, bounded so a report stays storable.
MAX_EXCERPT_CHARS: Final = 4000


class ScoreComponent(StrEnum):
    """The seven rows of 02_ARCHITECTURE.md §11, and nothing else."""

    REGISTRATION_AND_DISCOVERY = "REGISTRATION_AND_DISCOVERY"
    SCHEMA_VALIDITY = "SCHEMA_VALIDITY"
    EXECUTION = "EXECUTION"
    ERROR_HANDLING = "ERROR_HANDLING"
    UI_SYNCHRONIZATION = "UI_SYNCHRONIZATION"
    AUTHORIZATION_SAFETY = "AUTHORIZATION_SAFETY"
    REGRESSION = "REGRESSION"

    @property
    def label(self) -> str:
        """The component's name **exactly as §11 writes it**.

        The document is the source of the weights, so the label is how a test
        matches a row of that table to a member of this enum. Changing one here
        without changing the document fails that test.
        """
        return _LABELS[self]


_LABELS: Final[dict[ScoreComponent, str]] = {
    ScoreComponent.REGISTRATION_AND_DISCOVERY: "Tool registration & discovery",
    ScoreComponent.SCHEMA_VALIDITY: "Schema validity",
    ScoreComponent.EXECUTION: "Successful execution",
    ScoreComponent.ERROR_HANDLING: "Error handling (invalid input rejected correctly)",
    ScoreComponent.UI_SYNCHRONIZATION: "UI state synchronization",
    ScoreComponent.AUTHORIZATION_SAFETY: "Authorization & approval safety",
    ScoreComponent.REGRESSION: "Regression tests still passing",
}

#: 02_ARCHITECTURE.md §11. These sum to `TOTAL_POINTS`, checked at import.
COMPONENT_WEIGHTS: Final[dict[ScoreComponent, int]] = {
    ScoreComponent.REGISTRATION_AND_DISCOVERY: 20,
    ScoreComponent.SCHEMA_VALIDITY: 15,
    ScoreComponent.EXECUTION: 25,
    ScoreComponent.ERROR_HANDLING: 10,
    ScoreComponent.UI_SYNCHRONIZATION: 10,
    ScoreComponent.AUTHORIZATION_SAFETY: 15,
    ScoreComponent.REGRESSION: 5,
}

TOTAL_POINTS: Final = 100


class ScoreTableError(Exception):
    """The weight table does not describe a score out of `TOTAL_POINTS`.

    Raised at import, because a score computed from an incoherent table is
    worse than no score. There is no runtime path that repairs it.
    """


def _check_weight_table() -> None:
    missing = set(ScoreComponent) - set(COMPONENT_WEIGHTS)
    if missing:
        raise ScoreTableError(f"no weight for {sorted(c.value for c in missing)}")
    extra = set(COMPONENT_WEIGHTS) - set(ScoreComponent)
    if extra:
        raise ScoreTableError(f"weight for something that is not a component: {sorted(extra)}")
    if any(w < 0 for w in COMPONENT_WEIGHTS.values()):
        raise ScoreTableError("a component weight is negative")
    total = sum(COMPONENT_WEIGHTS.values())
    if total != TOTAL_POINTS:
        raise ScoreTableError(f"the component weights sum to {total}, not {TOTAL_POINTS}")
    unlabelled = set(ScoreComponent) - set(_LABELS)
    if unlabelled:
        raise ScoreTableError(f"no §11 label for {sorted(c.value for c in unlabelled)}")


_check_weight_table()


class CheckEvidence(BaseModel):
    """What one executed command did. The only thing a score is made of.

    There is no `passed` field. It is derived from the exit code every time it
    is asked for, so a stored report re-scores to the same number and no caller
    can persist a verdict that disagrees with the command that produced it.
    """

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1, max_length=120)
    argv: tuple[str, ...]
    exit_code: int
    timed_out: bool
    duration_seconds: float
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    output_truncated: bool = False

    @property
    def passed(self) -> bool:
        """A command passed if it exited zero and was not killed by the clock.

        Nothing here reads the output. A command that prints "PASS" and exits 1
        failed, and a command that prints an apology and exits 0 passed —
        asserted by `test_the_verdict_comes_from_the_exit_code_not_the_output`.
        """
        return self.exit_code == 0 and not self.timed_out

    @classmethod
    def from_result(cls, check_id: str, result: CommandResult) -> CheckEvidence:
        """The one place evidence is built, and it is built from a real result.

        Two checks, and the guarantee is their conjunction rather than either
        alone.

        `test_check_evidence_is_constructed_in_exactly_one_place` sweeps every
        backend module for a second `CheckEvidence(...)` call site, and
        `test_no_module_assembles_evidence_from_a_mapping` sweeps for an
        `ExecutedCheck(...)` built with a dict literal for `evidence`. The
        second exists because pydantic coerces a mapping into this model, so a
        hand-assembled dict becomes a passing record with **no call site for
        the first sweep to find** — measured: such a dict scored the full 25
        EXECUTION points. The sweep alone was never the guarantee an earlier
        version of this docstring claimed it was.

        **Stated bound.** Both match names and shapes in the AST — bare and
        attribute-qualified calls alike — so they catch straightforwardly-written
        code and do not defeat deliberate indirection: `getattr`, an aliased
        import, or a dict assembled elsewhere and passed in as a variable.
        Coercion is deliberately still allowed, because `ValidationReport` must
        round-trip through serialisation and
        `test_the_report_round_trips_through_serialisation` pins that.

        The class is named explicitly rather than using `cls`, so the sweep has
        a name to match; `CheckEvidence` is frozen and is not subclassed.
        """
        del cls
        return CheckEvidence(
            check_id=check_id,
            argv=tuple(result.argv),
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_seconds=result.duration_seconds,
            stdout_excerpt=result.stdout[-MAX_EXCERPT_CHARS:],
            stderr_excerpt=result.stderr[-MAX_EXCERPT_CHARS:],
            output_truncated=result.output_truncated,
        )


class ExecutedCheck(BaseModel):
    """One check that ran, and the component its result feeds.

    `component` is `None` for a check that is recorded but scores nothing —
    build and typecheck, which §11 gives no points to. They still gate the
    report's verdict.
    """

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1, max_length=120)
    component: ScoreComponent | None
    description: str = Field(min_length=1, max_length=300)
    evidence: CheckEvidence

    @property
    def passed(self) -> bool:
        return self.evidence.passed


class SkippedCheck(BaseModel):
    """A check that could not be executed, and why.

    A skipped check produces **no evidence**, so it contributes nothing to its
    component — which is how "no evidence scores zero" behaves when a command
    could not run at all, rather than when it ran and failed.
    """

    model_config = ConfigDict(frozen=True)

    check_id: str = Field(min_length=1, max_length=120)
    component: ScoreComponent | None
    description: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=400)


class ComponentScore(BaseModel):
    """One row of the report. Constructed only by `score_components`."""

    model_config = ConfigDict(frozen=True)

    component: ScoreComponent
    label: str
    weight: int
    points: int
    checks_passed: int
    checks_executed: int
    check_ids: tuple[str, ...] = ()
    evidence: tuple[CheckEvidence, ...] = ()
    detail: str = ""

    @property
    def has_evidence(self) -> bool:
        return self.checks_executed > 0


class ReadinessScore(BaseModel):
    """The Agent Readiness Score, and every row that produced it."""

    model_config = ConfigDict(frozen=True)

    components: tuple[ComponentScore, ...]
    total: int
    max_total: int = TOTAL_POINTS
    #: Checks that ran and are recorded but score nothing (build, typecheck).
    ungraded: tuple[ExecutedCheck, ...] = ()

    def component(self, component: ScoreComponent) -> ComponentScore:
        for row in self.components:
            if row.component is component:
                return row
        raise KeyError(component)

    @property
    def components_without_evidence(self) -> tuple[ScoreComponent, ...]:
        return tuple(row.component for row in self.components if not row.has_evidence)


NO_EVIDENCE_DETAIL: Final = "No check produced evidence for this component; it scores zero."


def score_components(checks: Sequence[ExecutedCheck]) -> ReadinessScore:
    """Compute the score from executed checks. The only producer of points.

    A component's points are `weight * passed / executed`, rounded **down**. A
    component with no executed check scores zero and carries
    `NO_EVIDENCE_DETAIL`, never a default and never a partial credit.
    """
    rows: list[ComponentScore] = []
    for component in ScoreComponent:
        weight = COMPONENT_WEIGHTS[component]
        executed = [c for c in checks if c.component is component]
        passed = [c for c in executed if c.passed]

        if not executed:
            points = 0
            detail = NO_EVIDENCE_DETAIL
        else:
            points = weight * len(passed) // len(executed)
            detail = f"{len(passed)}/{len(executed)} checks passed."

        rows.append(
            ComponentScore(
                component=component,
                label=component.label,
                weight=weight,
                points=points,
                checks_passed=len(passed),
                checks_executed=len(executed),
                check_ids=tuple(c.check_id for c in executed),
                evidence=tuple(c.evidence for c in executed),
                detail=detail,
            )
        )

    return ReadinessScore(
        components=tuple(rows),
        total=sum(row.points for row in rows),
        ungraded=tuple(c for c in checks if c.component is None),
    )
