"""The deterministic policy engine — ticket F6-01, 03_SECURITY_ACCESS.md §8, §11.

Code-based checks over a generated patch and its tool plan. Every rule here is
decidable without judgement, which is the point: an agent's opinion is advisory
input, and a violation found here blocks regardless of what any model said.

The rules are data (`RULES`), so the set can be read, counted and tested rather
than being scattered through branches.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from mcpforge.agents.architect import infer_risk_from_function
from mcpforge.models.analysis import Evidence
from mcpforge.models.patch import GeneratedPatch
from mcpforge.models.security import Finding, Severity
from mcpforge.models.toolplan import FORBIDDEN_PARAMETER_NAMES, ToolPlan, ToolPlanEntry
from mcpforge.security.filters import classify_path, scan_content

#: Paths a generated patch must never touch, whatever it claims to be doing.
#: Wider than `FileChange`'s own guard: that one stops escapes, this one stops a
#: patch quietly rewriting the developer's CI, dependencies or configuration.
SENSITIVE_PATHS: tuple[str, ...] = (
    ".github/",
    ".git/",
    ".husky/",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "tsconfig.json",
    "next.config",
    "middleware.ts",
    "middleware.js",
    "Dockerfile",
    "docker-compose",
    ".env",
)


@dataclass(frozen=True)
class PolicyContext:
    """Everything a rule may look at."""

    plan: ToolPlan
    patch: GeneratedPatch | None = None


Rule = Callable[[PolicyContext], list[Finding]]


def _evidence_for(tool: ToolPlanEntry) -> Evidence | None:
    return (
        Evidence(path=tool.evidence[0].path, symbol=tool.maps_to_function)
        if tool.evidence
        else None
    )


# ---------------------------------------------------------------------------
# Tool-plan rules
# ---------------------------------------------------------------------------


def rule_state_change_requires_approval(context: PolicyContext) -> list[Finding]:
    """§8.1. Risk is re-derived here; the model's field is not trusted."""
    findings: list[Finding] = []
    for tool in context.plan.tools:
        inferred = infer_risk_from_function(tool.maps_to_function)
        enforced = tool.risk if tool.risk.rank >= inferred.rank else inferred

        if enforced.requires_approval and not tool.approval_required:
            claimed = (
                f" The plan claimed {tool.risk.value}; "
                f"'{tool.maps_to_function}' reads as {inferred.value}."
                if enforced is not tool.risk
                else ""
            )
            findings.append(
                Finding(
                    rule="approval-required-for-state-change",
                    severity=Severity.CRITICAL,
                    summary=(
                        f"Tool '{tool.name}' is {enforced.value} but is not gated by "
                        f"human approval.{claimed}"
                    ),
                    recommendation="Gate it behind approval, or reduce what the tool does.",
                    evidence=_evidence_for(tool),
                    deterministic=True,
                )
            )
    return findings


def rule_no_forbidden_parameters(context: PolicyContext) -> list[Finding]:
    """§8.2. A parameter that selects a table, path, user or permission hands an
    agent authority the application never gave the caller."""
    findings: list[Finding] = []
    for tool in context.plan.tools:
        forbidden = sorted(
            p.name for p in tool.parameters if p.name.lower() in FORBIDDEN_PARAMETER_NAMES
        )
        if forbidden:
            findings.append(
                Finding(
                    rule="forbidden-parameter",
                    severity=Severity.CRITICAL,
                    summary=(
                        f"Tool '{tool.name}' accepts {forbidden}, which grants authority "
                        "the application never gave the caller."
                    ),
                    recommendation="Remove it; derive the value from the session instead.",
                    evidence=_evidence_for(tool),
                    deterministic=True,
                )
            )
    return findings


def rule_tools_cite_evidence(context: PolicyContext) -> list[Finding]:
    findings: list[Finding] = []
    for tool in context.plan.tools:
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


def rule_tool_names_are_unique(context: PolicyContext) -> list[Finding]:
    """Two tools sharing a name is ambiguous to an agent and to the generator."""
    names = [t.name for t in context.plan.tools]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    return [
        Finding(
            rule="duplicate-tool-name",
            severity=Severity.HIGH,
            summary=f"Tool name '{name}' is used more than once.",
            recommendation="Give each tool a distinct, intent-level name.",
            deterministic=True,
        )
        for name in duplicates
    ]


# ---------------------------------------------------------------------------
# Patch rules
# ---------------------------------------------------------------------------


def rule_no_secrets_in_generated_content(context: PolicyContext) -> list[Finding]:
    """§4.4. Scanned before review and again before a pull request."""
    if context.patch is None:
        return []
    findings: list[Finding] = []
    for change in context.patch.files:
        hits = scan_content(change.contents)
        if hits:
            rules = sorted({h.rule for h in hits})
            findings.append(
                Finding(
                    rule="secret-in-generated-content",
                    severity=Severity.CRITICAL,
                    summary=(
                        f"{change.path} contains credential-shaped content: {', '.join(rules)}."
                    ),
                    recommendation="Remove it. A credential must never reach a pull request.",
                    evidence=Evidence(path=change.path),
                    deterministic=True,
                )
            )
    return findings


def rule_no_sensitive_paths(context: PolicyContext) -> list[Finding]:
    """A generated patch adds WebMCP files. It does not touch CI, dependencies,
    configuration or middleware, and a patch that tries to is refused."""
    if context.patch is None:
        return []
    findings: list[Finding] = []
    for change in context.patch.files:
        matched = [p for p in SENSITIVE_PATHS if change.path.startswith(p) or p in change.path]
        if matched:
            findings.append(
                Finding(
                    rule="sensitive-path",
                    severity=Severity.CRITICAL,
                    summary=(
                        f"{change.path} touches {matched[0]}, which a generated patch "
                        "must never modify."
                    ),
                    recommendation="Generate only into the WebMCP directory.",
                    evidence=Evidence(path=change.path),
                    deterministic=True,
                )
            )
    return findings


def rule_patch_only_adds_files(context: PolicyContext) -> list[Finding]:
    """Phase 5 generates new files. A modification would rewrite the developer's
    own code, and nothing in the pipeline is allowed to do that yet."""
    if context.patch is None:
        return []
    return [
        Finding(
            rule="modifies-existing-file",
            severity=Severity.HIGH,
            summary=f"{change.path} modifies an existing file rather than adding one.",
            recommendation="Generated integration adds files; it does not rewrite source.",
            evidence=Evidence(path=change.path),
            deterministic=True,
        )
        for change in context.patch.files
        if change.kind.value != "add"
    ]


def rule_no_quarantined_paths(context: PolicyContext) -> list[Finding]:
    """A path the secret filter would quarantine must not be written either."""
    if context.patch is None:
        return []
    findings: list[Finding] = []
    for change in context.patch.files:
        decision = classify_path(change.path, size_bytes=len(change.contents))
        if decision.verdict.is_secret:
            findings.append(
                Finding(
                    rule="writes-credential-path",
                    severity=Severity.CRITICAL,
                    summary=f"{change.path} is a credential path ({decision.reason}).",
                    recommendation="Never generate a file at a path that holds credentials.",
                    evidence=Evidence(path=change.path),
                    deterministic=True,
                )
            )
    return findings


def rule_every_tool_has_generated_code(context: PolicyContext) -> list[Finding]:
    """A plan the patch does not implement means someone approved one thing and
    would receive another."""
    if context.patch is None:
        return []
    blob = "\n".join(f.path for f in context.patch.files)
    return [
        Finding(
            rule="tool-not-generated",
            severity=Severity.HIGH,
            summary=f"Tool '{tool.name}' appears in the plan but not in the patch.",
            recommendation="Regenerate, or remove the tool from the plan.",
            evidence=_evidence_for(tool),
            deterministic=True,
        )
        for tool in context.plan.tools
        if _handler_name(tool.name) not in blob
    ]


def _handler_name(tool_name: str) -> str:
    head, *rest = tool_name.split("_")
    return head + "".join(part.capitalize() for part in rest)


#: Every rule, as data. Adding one here is the only way to add a policy check.
RULES: tuple[Rule, ...] = (
    rule_state_change_requires_approval,
    rule_no_forbidden_parameters,
    rule_tools_cite_evidence,
    rule_tool_names_are_unique,
    rule_no_secrets_in_generated_content,
    rule_no_sensitive_paths,
    rule_patch_only_adds_files,
    rule_no_quarantined_paths,
    rule_every_tool_has_generated_code,
)


def evaluate_policy(plan: ToolPlan, patch: GeneratedPatch | None = None) -> list[Finding]:
    """Run every rule. Order is stable so findings read the same way twice."""
    context = PolicyContext(plan=plan, patch=patch)
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(context))
    return findings
