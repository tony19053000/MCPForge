"""Agent 2 — Workflow Architect.

Turns selected workflows into WebMCP tool proposals. The interesting part is not
the model call: it is `reconcile_risk`, which re-derives each tool's risk from
the function it maps to and takes the stricter of the two verdicts.

03_SECURITY_ACCESS.md §8.1: "If the agent's classification and the deterministic
check disagree, the stricter one wins and the discrepancy is surfaced as a
finding." A model that under-classifies `cancelReservation` as READ must not be
able to remove an approval gate by saying so.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from mcpforge.agents.base import Agent, AgentEvidenceError
from mcpforge.models.analysis import CodebaseAnalysis, RiskClass, Workflow
from mcpforge.models.index import RepositoryIndex
from mcpforge.models.toolplan import ToolPlan, ToolPlanEntry

SYSTEM_INSTRUCTION = """
You are the MCPForge Workflow Architect.

You are given workflows a developer has chosen to expose to AI agents. Design one
WebMCP tool for each.

Rules:
- Name tools at the level of intent, in snake_case: `search_rooms`,
  `cancel_reservation`. Never UI mechanics like `click_button` or `submit_form`.
- `maps_to_function` must be the existing function that already implements the
  workflow. Generated code will call it. Never invent a new one.
- Parameters are the business inputs a caller needs. Never accept a table name,
  a file path, a URL, a SQL fragment, a user id, a role, a permission, or a
  token: the application already knows who the caller is, and a tool that takes
  those hands an agent authority the application never granted it.
- Classify risk honestly. READ changes nothing. WRITE creates or modifies state.
  DESTRUCTIVE deletes, cancels, charges, or is otherwise irreversible.
- You have no authority to approve anything.

Respond only with the requested JSON.
""".strip()

#: Function-name shapes that betray a state change regardless of what the model
#: called it. Deliberately blunt: over-classifying costs an approval click,
#: under-classifying costs a silent destructive tool.
DESTRUCTIVE_VERBS = (
    "delete",
    "remove",
    "destroy",
    "cancel",
    "revoke",
    "terminate",
    "drop",
    "purge",
    "wipe",
    "refund",
    "charge",
    "close",
)
WRITE_VERBS = (
    "create",
    "update",
    "insert",
    "add",
    "set",
    "save",
    "write",
    "put",
    "post",
    "modify",
    "edit",
    "patch",
    "book",
    "reserve",
    "submit",
    "send",
    "register",
    "upsert",
    "apply",
    "confirm",
    "approve",
    "assign",
)


def infer_risk_from_function(function_name: str) -> RiskClass:
    """Derive risk from a function's name, independently of the model.

    Names are weak evidence, which is why this only ever *raises* the risk.
    """
    words = re.split(r"[^a-z]+", function_name.lower())
    if any(w.startswith(DESTRUCTIVE_VERBS) for w in words if w):
        return RiskClass.DESTRUCTIVE
    if any(w.startswith(WRITE_VERBS) for w in words if w):
        return RiskClass.WRITE
    return RiskClass.READ


class RiskDiscrepancy(BaseModel):
    """Recorded whenever the model and the deterministic check disagree."""

    tool: str
    proposed: RiskClass
    enforced: RiskClass
    reason: str


class ArchitectInput(BaseModel):
    index: RepositoryIndex
    analysis: CodebaseAnalysis
    #: Workflow ids the developer selected. Only these become tools.
    selected_workflow_ids: list[str] = Field(min_length=1)

    def selected(self) -> list[Workflow]:
        chosen = set(self.selected_workflow_ids)
        return [w for w in self.analysis.workflows if w.id in chosen]


class WorkflowArchitect(Agent[ArchitectInput, ToolPlan]):
    name = "architect"
    step = "Designing WebMCP tools"
    output_model = ToolPlan

    def system_instruction(self) -> str:
        return SYSTEM_INSTRUCTION

    def build_prompt(self, payload: ArchitectInput) -> str:
        lines = ["Workflows the developer selected:", ""]
        for workflow in payload.selected():
            lines += [
                f"- id: {workflow.id}",
                f"  name: {workflow.name}",
                f"  description: {workflow.description}",
                f"  implemented by: {workflow.primary_function}",
                f"  files: {', '.join(e.path for e in workflow.evidence)}",
            ]

        lines += ["", "Functions available to call, with their parameters:"]
        for file in payload.index.services:
            for symbol in file.symbols:
                if symbol.exported and symbol.kind.value == "function":
                    lines.append(
                        f"  {symbol.name}({', '.join(symbol.params)})  [{file.path}:{symbol.line}]"
                    )
        return "\n".join(lines)

    def verify(self, output: ToolPlan, payload: ArchitectInput) -> None:
        """Reject a plan that cannot be generated from.

        Checked here rather than at generation time, because a tool naming a
        function that does not exist produces code that cannot compile, and the
        cheapest place to catch that is before a human is asked to approve it.
        """
        index = payload.index
        selected = {w.id for w in payload.selected()}
        problems: list[str] = []

        if not output.tools:
            problems.append("the plan contains no tools")

        seen: set[str] = set()
        for tool in output.tools:
            if tool.name in seen:
                problems.append(f"duplicate tool name '{tool.name}'")
            seen.add(tool.name)

            if index.find_symbol(tool.maps_to_function) is None:
                problems.append(
                    f"tool '{tool.name}' maps to '{tool.maps_to_function}', which does not exist"
                )
            if tool.workflow_id not in selected:
                problems.append(
                    f"tool '{tool.name}' targets workflow '{tool.workflow_id}', "
                    "which the developer did not select"
                )
            forbidden = tool.forbidden_parameters()
            if forbidden:
                problems.append(
                    f"tool '{tool.name}' takes forbidden parameter(s) {forbidden}: "
                    "these grant authority the application never gave the caller"
                )
            for evidence in tool.evidence:
                if evidence.path not in {f.path for f in index.files}:
                    problems.append(
                        f"tool '{tool.name}' cites {evidence.path}, which is not in the index"
                    )

        if problems:
            raise AgentEvidenceError("Tool plan rejected: " + "; ".join(problems[:5]))


def reconcile_risk(plan: ToolPlan) -> tuple[ToolPlan, list[RiskDiscrepancy]]:
    """Take the stricter of the model's risk and the deterministic one.

    Also sets `approval_required`, which is derived here and never taken from
    the model. 03_SECURITY_ACCESS.md §8.1.
    """
    discrepancies: list[RiskDiscrepancy] = []
    reconciled: list[ToolPlanEntry] = []

    for tool in plan.tools:
        inferred = infer_risk_from_function(tool.maps_to_function)
        enforced = tool.risk if tool.risk.rank >= inferred.rank else inferred

        if enforced is not tool.risk:
            discrepancies.append(
                RiskDiscrepancy(
                    tool=tool.name,
                    proposed=tool.risk,
                    enforced=enforced,
                    reason=(
                        f"'{tool.maps_to_function}' reads as {inferred.value} from its name, "
                        f"but the plan claimed {tool.risk.value}. The stricter class wins."
                    ),
                )
            )

        reconciled.append(
            tool.model_copy(
                update={"risk": enforced, "approval_required": enforced.requires_approval}
            )
        )

    return plan.model_copy(update={"tools": reconciled}), discrepancies
