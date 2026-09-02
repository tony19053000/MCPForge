"""Agent 4 — Security Reviewer, and the deterministic gate above it.

The agent reviews a generated tool plan or patch and reports findings. Its
verdict is advisory: `evaluate_gate` combines it with our own policy checks and
takes the pessimistic view. An agent that says PASS cannot clear a violation
code found, and an agent that says FAIL is believed.

03_SECURITY_ACCESS.md §7: "The Security Reviewer agent's PASS is advisory input
to a deterministic gate that also applies our own policy checks. An agent PASS
cannot clear a policy violation found by code."
"""

from __future__ import annotations

from pydantic import BaseModel

from mcpforge.agents.architect import infer_risk_from_function
from mcpforge.agents.base import Agent
from mcpforge.models.analysis import Evidence
from mcpforge.models.security import Finding, GateVerdict, SecurityReport, Severity
from mcpforge.models.toolplan import ToolPlan

SYSTEM_INSTRUCTION = """
You are the MCPForge Security Reviewer.

You are given a plan of WebMCP tools that will be exposed to AI agents. Report
anything unsafe about it.

Look for: tools that can change or destroy state without an approval gate; tools
that accept a parameter granting authority the application never gave the caller
(identifiers, roles, paths, queries, tokens); tools whose description understates
what they do; missing validation; and anything that would let an agent act
beyond the workflow it was designed for.

Rules:
- Report findings with a severity. HIGH and CRITICAL block generation.
- Your verdict is advisory. Deterministic checks run alongside you and the
  stricter result wins, so err towards reporting.
- You have no authority to approve anything.

Respond only with the requested JSON.
""".strip()


class SecurityReviewInput(BaseModel):
    plan: ToolPlan


class SecurityReviewer(Agent[SecurityReviewInput, SecurityReport]):
    name = "security_reviewer"
    step = "Running security review"
    output_model = SecurityReport

    def system_instruction(self) -> str:
        return SYSTEM_INSTRUCTION

    def build_prompt(self, payload: SecurityReviewInput) -> str:
        lines = ["Proposed WebMCP tools:", ""]
        for tool in payload.plan.tools:
            params = ", ".join(f"{p.name}: {p.json_type}" for p in tool.parameters) or "none"
            lines += [
                f"- {tool.name}",
                f"    description       : {tool.description}",
                f"    calls             : {tool.maps_to_function}",
                f"    parameters        : {params}",
                f"    risk              : {tool.risk.value}",
                f"    approval required : {tool.approval_required}",
            ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The deterministic half
# ---------------------------------------------------------------------------


def policy_findings(plan: ToolPlan) -> list[Finding]:
    """Our own checks, run regardless of what the agent said.

    Deliberately narrow and mechanical: each one is a rule from
    03_SECURITY_ACCESS.md §8 that can be decided without judgement.

    Crucially, risk is **re-derived here** rather than read from `tool.risk`.
    Both `risk` and `approval_required` are fields the model fills, so a check
    that trusted them would be deterministic in name only: an unreconciled plan
    marking `cancelReservation` as READ with no approval would sail through.
    This function does not assume `reconcile_risk` has run.
    """
    findings: list[Finding] = []

    for tool in plan.tools:
        inferred = infer_risk_from_function(tool.maps_to_function)
        enforced = tool.risk if tool.risk.rank >= inferred.rank else inferred

        if enforced.requires_approval and not tool.approval_required:
            findings.append(
                Finding(
                    rule="approval-required-for-state-change",
                    severity=Severity.CRITICAL,
                    summary=(
                        f"Tool '{tool.name}' is {enforced.value} but is not gated by "
                        "human approval."
                        + (
                            f" The plan claimed {tool.risk.value}; "
                            f"'{tool.maps_to_function}' reads as {inferred.value}."
                            if enforced is not tool.risk
                            else ""
                        )
                    ),
                    recommendation="Set approval_required, or reduce what the tool does.",
                    evidence=Evidence(path=tool.evidence[0].path, symbol=tool.maps_to_function),
                    deterministic=True,
                )
            )

        forbidden = tool.forbidden_parameters()
        if forbidden:
            findings.append(
                Finding(
                    rule="forbidden-parameter",
                    severity=Severity.CRITICAL,
                    summary=(
                        f"Tool '{tool.name}' accepts {forbidden}, which grants authority "
                        "the application never gave the caller."
                    ),
                    recommendation="Remove the parameter; derive it from the session instead.",
                    evidence=Evidence(path=tool.evidence[0].path, symbol=tool.maps_to_function),
                    deterministic=True,
                )
            )

        if not tool.evidence:
            findings.append(
                Finding(
                    rule="no-evidence",
                    severity=Severity.HIGH,
                    summary=f"Tool '{tool.name}' cites no source file.",
                    recommendation="Map the tool to the function that implements it.",
                    deterministic=True,
                )
            )

    return findings


def evaluate_gate(report: SecurityReport, plan: ToolPlan) -> GateVerdict:
    """Combine the agent's report with our own policy checks.

    The orchestrator reads this, never the agent's `advisory_pass`.
    """
    deterministic = policy_findings(plan)
    combined = [*deterministic, *report.findings]
    blocking = [f for f in combined if f.severity.blocks]
    passed = not blocking

    overridden = report.advisory_pass and not passed
    if overridden:
        blocked_by = ", ".join(sorted({f.rule for f in blocking}))
        reason = (
            f"The security agent reported PASS, but {len(blocking)} blocking finding(s) "
            f"stand: {blocked_by}. Deterministic checks decide the gate."
        )
    elif not report.advisory_pass and passed:
        reason = (
            "The security agent reported FAIL but raised nothing blocking. Its concerns "
            "are recorded as findings; the gate is open."
        )
    elif passed:
        reason = "No blocking findings from either the agent or the policy engine."
    else:
        reason = f"{len(blocking)} blocking finding(s)."

    return GateVerdict(
        passed=passed,
        findings=combined,
        agent_said_pass=report.advisory_pass,
        overridden=overridden,
        reason=reason,
    )
