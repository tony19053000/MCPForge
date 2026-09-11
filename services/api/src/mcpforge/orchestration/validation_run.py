"""Running agent 5 for the pipeline — F9-01.

Its own module for one reason: `orchestration/pipeline.py` reaches Gemini (it
constructs agents 1, 2 and 4), and **no module may both reach Gemini and touch
the readiness score** (`test_no_module_both_prompts_gemini_and_touches_the_score`,
F8-04). This module holds the validator and its report and cannot reach a
model; the pipeline receives only the verdict, a one-line summary and the
serialized report to store. It never holds a `ValidationReport`, so there is no
object in the pipeline a model's answer could be written into.

**What a pipeline pass requires.** `ValidationReport.passed` says only that the
checks which *ran* passed — correct for the report, which shows its skips and
scores them zero. It is not enough to open a gate on: a workspace with no
dependency tree skips every tool check, runs typecheck and build, and "passes"
with a score of zero. So a pipeline pass also requires that every check that
exercises the generated tools actually executed — registration, schema,
execution or authorization, and error handling — for every tool. The one
permitted skip is error handling for a tool with no inputs, where the generator
emits no rejection test (`NO_INPUTS_SKIP_REASON`). The application's own
`test`, `typecheck` and `build` scripts are not required, because an
application may declare none, and UI synchronization has no executable check
at all. Pinned by `test_a_repository_with_no_dependency_tree_cannot_pass_validation`
and `test_a_pass_requires_every_tool_check_to_have_run`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcpforge.agents.validator import (
    NO_INPUTS_SKIP_REASON,
    ValidationReport,
    Validator,
    ValidatorError,
)
from mcpforge.execution.provider import SecureExecutionProvider, Workspace
from mcpforge.models.webmcp import WebMCPToolset

#: Check-id prefixes of the checks that exercise the generated tools. Every
#: planned check with one of these must have executed for a pipeline pass.
TOOL_CHECK_PREFIXES: tuple[str, ...] = (
    "registration:",
    "schema:",
    "execution:",
    "authorization:",
    "error_handling:",
)


class ValidationCouldNotRunError(Exception):
    """The validator refused to run at all. No evidence exists, so no verdict."""


@dataclass(frozen=True)
class ValidationOutcome:
    """What the pipeline needs, and nothing it could use to set a score."""

    passed: bool
    summary: str
    failed_check_ids: tuple[str, ...]
    #: Tool checks that were planned and did not run. Non-empty means the
    #: integration was not validated, whatever the executed checks said.
    unexecuted: tuple[str, ...]
    #: Stored as the `VALIDATION` artifact. Serialized here, from the report.
    payload: dict[str, Any]


def unexecuted_tool_checks(report: ValidationReport) -> tuple[str, ...]:
    """The tool checks that were skipped for any reason but the one permitted."""
    return tuple(
        skipped.check_id
        for skipped in report.skipped
        if skipped.check_id.startswith(TOOL_CHECK_PREFIXES)
        and not (
            skipped.check_id.startswith("error_handling:")
            and skipped.reason == NO_INPUTS_SKIP_REASON
        )
    )


def outcome_of(report: ValidationReport) -> ValidationOutcome:
    unexecuted = unexecuted_tool_checks(report)
    summary = (
        f"Agent Readiness {report.score.total}/100 from {len(report.checks)} executed "
        f"check(s), {len(report.skipped)} skipped"
    )
    if unexecuted:
        summary += f"; not validated — {len(unexecuted)} tool check(s) did not run"
    return ValidationOutcome(
        passed=report.passed and not unexecuted,
        summary=summary,
        failed_check_ids=report.failed_check_ids,
        unexecuted=unexecuted,
        payload={
            "completed": True,
            "validated": not unexecuted,
            "unexecuted_tool_checks": list(unexecuted),
            "report": report.model_dump(mode="json"),
        },
    )


async def validate_workspace(
    executor: SecureExecutionProvider,
    workspace: Workspace,
    toolset: WebMCPToolset,
    *,
    app_dir: str,
    timeout_seconds: int,
) -> ValidationOutcome:
    """Run agent 5 over a prepared workspace and return its outcome."""
    try:
        report = await Validator(executor, timeout_seconds=timeout_seconds).validate(
            workspace, toolset, app_dir=app_dir
        )
    except ValidatorError as exc:
        raise ValidationCouldNotRunError(str(exc)) from exc
    return outcome_of(report)
