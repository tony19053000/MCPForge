"""What a pipeline validation pass requires — F9-01 round 1.

`ValidationReport.passed` means "every check that ran, passed". That is right
for the report and not enough to open a gate on. These tests pin the rule in
`orchestration/validation_run.py`: a pass also needs every tool check to have
executed, with one permitted skip.
"""

from __future__ import annotations

import pytest

from mcpforge.agents.validator import NO_INPUTS_SKIP_REASON, ValidationReport
from mcpforge.execution.provider import CommandResult
from mcpforge.orchestration.scoring import (
    CheckEvidence,
    ExecutedCheck,
    ScoreComponent,
    SkippedCheck,
    score_components,
)
from mcpforge.orchestration.validation_run import TOOL_CHECK_PREFIXES, outcome_of

C = ScoreComponent

#: One tool's full check set, as `plan_checks` produces it, plus the scripts.
TOOL_CHECKS: dict[str, ScoreComponent | None] = {
    "registration:search_rooms": C.REGISTRATION_AND_DISCOVERY,
    "schema:search_rooms": C.SCHEMA_VALIDITY,
    "execution:search_rooms": C.EXECUTION,
    "error_handling:search_rooms": C.ERROR_HANDLING,
    "authorization:cancel_reservation": C.AUTHORIZATION_SAFETY,
}
SCRIPT_CHECKS: dict[str, ScoreComponent | None] = {"typecheck": None, "build": None}


def _executed(check_id: str, component: ScoreComponent | None) -> ExecutedCheck:
    result = CommandResult(argv=("node",), exit_code=0, stdout="", stderr="", duration_seconds=0.1)
    return ExecutedCheck(
        check_id=check_id,
        component=component,
        description=check_id,
        evidence=CheckEvidence.from_result(check_id, result),
    )


def _skipped(check_id: str, component: ScoreComponent | None, reason: str) -> SkippedCheck:
    return SkippedCheck(check_id=check_id, component=component, description=check_id, reason=reason)


def _report(ran: dict[str, ScoreComponent | None], skipped: list[SkippedCheck]) -> ValidationReport:
    executed = [_executed(i, c) for i, c in ran.items()]
    return ValidationReport(
        checks=tuple(executed), skipped=tuple(skipped), score=score_components(executed)
    )


def test_every_tool_check_having_run_is_a_pass() -> None:
    outcome = outcome_of(_report({**TOOL_CHECKS, **SCRIPT_CHECKS}, []))
    assert outcome.passed is True
    assert outcome.unexecuted == ()


@pytest.mark.parametrize("missing", sorted(TOOL_CHECKS))
def test_a_pass_requires_every_tool_check_to_have_run(missing: str) -> None:
    """Skip each tool check in turn: every one alone is enough to refuse."""
    ran = {i: c for i, c in {**TOOL_CHECKS, **SCRIPT_CHECKS}.items() if i != missing}
    report = _report(ran, [_skipped(missing, TOOL_CHECKS[missing], "vitest is not installed")])
    assert report.passed is True, "precondition: the report itself calls this a pass"
    outcome = outcome_of(report)
    assert outcome.passed is False
    assert outcome.unexecuted == (missing,)
    assert outcome.payload["validated"] is False


def test_the_round_one_shape_is_not_a_pass() -> None:
    """Typecheck and build ran; every tool check was skipped; score zero."""
    report = _report(
        SCRIPT_CHECKS,
        [_skipped(i, c, "node_modules/vitest is not present") for i, c in TOOL_CHECKS.items()],
    )
    assert report.passed is True and report.score.total == 0
    assert outcome_of(report).passed is False


def test_a_tool_with_no_inputs_may_skip_error_handling_and_nothing_else() -> None:
    ran = {
        i: c
        for i, c in {**TOOL_CHECKS, **SCRIPT_CHECKS}.items()
        if i != "error_handling:search_rooms"
    }
    permitted = _skipped("error_handling:search_rooms", C.ERROR_HANDLING, NO_INPUTS_SKIP_REASON)
    assert outcome_of(_report(ran, [permitted])).passed is True

    ran.pop("execution:search_rooms")
    with_reason_elsewhere = _skipped("execution:search_rooms", C.EXECUTION, NO_INPUTS_SKIP_REASON)
    assert outcome_of(_report(ran, [permitted, with_reason_elsewhere])).passed is False


def test_skipped_application_scripts_and_ui_sync_do_not_block_a_pass() -> None:
    """An application may declare no test script, and UI synchronization has no
    executable check at all — both already score zero, neither is a tool check."""
    report = _report(
        TOOL_CHECKS,
        [
            _skipped("regression", C.REGRESSION, "no 'test' script"),
            _skipped("ui_synchronization", C.UI_SYNCHRONIZATION, "no executable check"),
        ],
    )
    assert outcome_of(report).passed is True


def test_the_prefixes_cover_every_tool_check_the_validator_plans() -> None:
    """If the validator grows a new per-tool check, this rule must know it."""
    from mcpforge.agents.validator import ApplicationFacts, plan_checks
    from tests.test_validator import _toolset

    planned = plan_checks(_toolset(), ApplicationFacts(scripts=frozenset(), vitest_available=True))
    per_tool = {p.check_id for p in planned if ":" in p.check_id}
    assert per_tool, "the validator planned no per-tool checks"
    assert all(i.startswith(TOOL_CHECK_PREFIXES) for i in per_tool), sorted(per_tool)
