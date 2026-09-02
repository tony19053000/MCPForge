"""The deterministic policy engine — F6-01, 03_SECURITY_ACCESS.md §8, §11.

Every rule gets a positive and a negative case. These are what block a patch
regardless of what any agent said, so a rule with no test is a rule that can be
deleted without anyone noticing.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcpforge.generation.nextjs import generate_patch
from mcpforge.models.patch import ChangeKind, FileChange, GeneratedPatch
from mcpforge.models.security import Severity
from mcpforge.models.toolplan import ToolPlan
from mcpforge.models.webmcp import WebMCPToolset
from mcpforge.security.policy import RULES, evaluate_policy
from tests.test_generation import destructive_tool, read_tool


def plan_with(**over: Any) -> ToolPlan:
    tool: dict[str, Any] = {
        "name": "cancel_reservation",
        "title": "Cancel a reservation",
        "description": "Cancels a booking.",
        "workflow_id": "cancel",
        "maps_to_function": "cancelReservation",
        "parameters": [
            {"name": "reservationId", "json_type": "string", "description": "Booking id"}
        ],
        "output_description": "The cancelled reservation.",
        "risk": "DESTRUCTIVE",
        "evidence": [{"path": "src/lib/reservations.ts"}],
        "approval_required": True,
    }
    tool.update(over)
    return ToolPlan.model_validate({"tools": [tool], "notes": []})


def rules_fired(findings: list[Any]) -> set[str]:
    return {f.rule for f in findings}


def patch_with(*changes: FileChange) -> GeneratedPatch:
    return GeneratedPatch(files=list(changes), summary="test patch")


def file(**over: Any) -> FileChange:
    data: dict[str, Any] = {
        "path": "src/webmcp/tools/cancelReservation.ts",
        "kind": ChangeKind.ADD,
        "contents": "export const x = 1;",
        "rationale": "why",
    }
    data.update(over)
    return FileChange.model_validate(data)


# -- the clean case, so the rules below mean something ---------------------


def test_a_sound_plan_and_patch_raise_nothing() -> None:
    toolset = WebMCPToolset(tools=[read_tool(), destructive_tool()])
    patch = generate_patch(toolset)
    plan = ToolPlan.model_validate(
        {
            "tools": [
                {
                    "name": "search_rooms",
                    "title": "Search rooms",
                    "description": "Find rooms.",
                    "workflow_id": "search",
                    "maps_to_function": "searchRooms",
                    "parameters": [
                        {"name": "guests", "json_type": "integer", "description": "How many"}
                    ],
                    "output_description": "Rooms.",
                    "risk": "READ",
                    "evidence": [{"path": "src/lib/rooms.ts"}],
                    "approval_required": False,
                },
                {
                    "name": "cancel_reservation",
                    "title": "Cancel",
                    "description": "Cancel a booking.",
                    "workflow_id": "cancel",
                    "maps_to_function": "cancelReservation",
                    "parameters": [
                        {"name": "reservationId", "json_type": "string", "description": "Id"}
                    ],
                    "output_description": "Cancelled.",
                    "risk": "DESTRUCTIVE",
                    "evidence": [{"path": "src/lib/reservations.ts"}],
                    "approval_required": True,
                },
            ],
            "notes": [],
        }
    )
    assert evaluate_policy(plan, patch) == []


def test_every_rule_is_registered() -> None:
    """The set is data so it can be counted. A rule added without registering it
    would never run."""
    assert len(RULES) == 9


# -- plan rules -------------------------------------------------------------


def test_an_ungated_state_change_is_critical() -> None:
    findings = evaluate_policy(plan_with(approval_required=False))
    assert "approval-required-for-state-change" in rules_fired(findings)
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].deterministic is True


def test_risk_is_re_derived_not_read_from_the_plan() -> None:
    """A plan claiming READ for cancelReservation is escalated by the engine."""
    findings = evaluate_policy(plan_with(risk="READ", approval_required=False))
    finding = next(f for f in findings if f.rule == "approval-required-for-state-change")
    assert "claimed READ" in finding.summary
    assert "DESTRUCTIVE" in finding.summary


def test_a_gated_destructive_tool_raises_nothing() -> None:
    assert "approval-required-for-state-change" not in rules_fired(evaluate_policy(plan_with()))


@pytest.mark.parametrize("name", ["user_id", "table", "path", "role", "token", "sql"])
def test_a_forbidden_parameter_is_critical(name: str) -> None:
    findings = evaluate_policy(
        plan_with(parameters=[{"name": name, "json_type": "string", "description": "x"}])
    )
    assert "forbidden-parameter" in rules_fired(findings)


def test_a_tool_with_no_evidence_is_flagged() -> None:
    plan = plan_with()
    stripped = plan.model_copy(
        update={"tools": [plan.tools[0].model_copy(update={"evidence": []})]}
    )
    assert "no-evidence" in rules_fired(evaluate_policy(stripped))


def test_duplicate_tool_names_are_flagged() -> None:
    plan = plan_with()
    doubled = plan.model_copy(update={"tools": [plan.tools[0], plan.tools[0]]})
    assert "duplicate-tool-name" in rules_fired(evaluate_policy(doubled))


# -- patch rules ------------------------------------------------------------


def test_a_credential_in_generated_content_is_critical() -> None:
    leaked = "AKIA" + "IOSFODNN7EXAMPLE"
    findings = evaluate_policy(plan_with(), patch_with(file(contents=f"const key = '{leaked}';")))
    assert "secret-in-generated-content" in rules_fired(findings)


@pytest.mark.parametrize(
    "path",
    ["package.json", "tsconfig.json", "next.config.ts", "middleware.ts", "Dockerfile"],
)
def test_touching_a_sensitive_path_is_critical(path: str) -> None:
    """A generated patch adds WebMCP files. It does not rewrite CI, dependencies
    or configuration."""
    findings = evaluate_policy(plan_with(), patch_with(file(path=path)))
    assert "sensitive-path" in rules_fired(findings)


@pytest.mark.parametrize("path", [".github/workflows/deploy.yml", ".git/config"])
def test_repository_infrastructure_is_refused_a_layer_earlier(path: str) -> None:
    """These never reach the policy engine: FileChange refuses them outright, so
    a patch targeting CI cannot even be constructed. Both layers are asserted so
    neither can be removed on the assumption the other covers it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="repository infrastructure"):
        file(path=path)


def test_modifying_an_existing_file_is_flagged() -> None:
    findings = evaluate_policy(
        plan_with(),
        patch_with(file(kind=ChangeKind.MODIFY, original="old", contents="new")),
    )
    assert "modifies-existing-file" in rules_fired(findings)


def test_writing_to_a_credential_path_is_critical() -> None:
    findings = evaluate_policy(plan_with(), patch_with(file(path="src/webmcp/.env.local")))
    assert "writes-credential-path" in rules_fired(findings)


def test_a_tool_in_the_plan_but_not_the_patch_is_flagged() -> None:
    """Otherwise a human approves one thing and receives another."""
    findings = evaluate_policy(plan_with(), patch_with(file(path="src/webmcp/types.ts")))
    assert "tool-not-generated" in rules_fired(findings)


def test_patch_rules_do_not_fire_without_a_patch() -> None:
    """Plan review happens before generation, so patch rules must be inert then."""
    findings = evaluate_policy(plan_with())
    patch_rules = {
        "secret-in-generated-content",
        "sensitive-path",
        "modifies-existing-file",
        "writes-credential-path",
        "tool-not-generated",
    }
    assert rules_fired(findings) & patch_rules == set()


# -- severity ---------------------------------------------------------------


def test_every_finding_is_marked_deterministic() -> None:
    """So a reader can tell a code finding from a model's opinion."""
    findings = evaluate_policy(
        plan_with(approval_required=False), patch_with(file(path="package.json"))
    )
    assert findings
    assert all(f.deterministic for f in findings)


def test_critical_and_high_findings_block() -> None:
    assert Severity.CRITICAL.blocks is True
    assert Severity.HIGH.blocks is True
    assert Severity.MEDIUM.blocks is False
    assert Severity.LOW.blocks is False
