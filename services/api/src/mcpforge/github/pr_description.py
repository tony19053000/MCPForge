"""Pull-request title and body, built from the plan, the patch and stored artifacts.

F6-02 required the tools, the changed files and the validation results; T6 adds
the workflows mapped, the security-review result, readiness, warnings and
testing instructions. Every section is composed deterministically from a stored
artifact — the plan, the patch, the `SECURITY_REVIEW` and `VALIDATION`
payloads, the workflow selection — never from model prose, and the body is
scanned by the writer before it leaves (§4.4).

**No model text.** Tool descriptions and titles, workflow names and
descriptions, and every finding's summary and recommendation were written by a
model. None of them is rendered, escaped or otherwise (ticket T6: "No secret or
model text appears in the PR body"). A finding is shown by severity, rule id and
source only. The source label reads `Finding.deterministic`, which is trusted
only because `evaluate_gate` establishes it: the flag is part of the schema the
reviewer answers in, so the gate forces it to `False` on every model finding and
only the policy engine's findings leave the gate with `True`. A
model-chosen identifier (workflow id, rule id, function or parameter name) is
shown only when it matches `_IDENTIFIER`, a pattern with no whitespace, markup
or table delimiter; otherwise it is withheld. Nothing here renders a prompt, a
raw model response, or a command's output.

Free text that is rendered — skip reasons, unchecked-type sentences, score
details — is written by MCPForge's own deterministic code and
still goes through `_plain` (redaction, then markdown/HTML/mention/autolink
neutralisation). Every value in a table cell goes through `_code`, `_ident` or
`_plain`, all of which remove `|` and newlines, so no value can split a cell.

**Honesty.** A missing, incomplete or unreadable review or validation is
rendered as such and never as passed. The execution trust level is read from
the stored validation artifact; anything other than a recorded, non-development
level is stated as `DEVELOPMENT_ISOLATION`, and attestation is not claimed.

This module touches the readiness score and must never reach Gemini
(`test_no_module_both_prompts_gemini_and_touches_the_score`). The pipeline,
which does reach Gemini, hands it raw payloads and never a score object.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import ValidationError

from mcpforge.agents.validator import ValidationReport
from mcpforge.execution.attestation import TrustLevel
from mcpforge.logging import _redact_text
from mcpforge.models.analysis import Workflow
from mcpforge.models.patch import GeneratedPatch
from mcpforge.models.security import GateVerdict, Severity
from mcpforge.models.toolplan import PLAIN_IDENTIFIER, ToolParameter, ToolPlan, ToolPlanEntry

#: GitHub rejects a pull-request body longer than this.
GITHUB_BODY_LIMIT: Final = 65536

TRUNCATION_NOTE: Final = (
    "\n\n---\n\n**This description was truncated** to fit GitHub's "
    f"{GITHUB_BODY_LIMIT}-character limit. The full run record is in MCPForge."
)

#: Longest single model-authored string rendered.
_MAX_PLAIN: Final = 400

#: Zero-width space. Breaks a mention, issue reference or autolink invisibly.
_ZWSP: Final = "\u200b"


def _word(passed: bool) -> str:
    return "pass" if passed else "fail"


#: What a `|` becomes. Not a table delimiter, in or out of a code span.
_PIPE: Final = "\u00a6"

#: A model-chosen identifier that is safe to show. No whitespace, markup,
#: backtick or `|`, so it can be neither prose nor a way out of a table cell.
#: Shared with `orchestration/toolset.py`, which quotes parameter names in the
#: unchecked-type sentences this body renders.
_IDENTIFIER = PLAIN_IDENTIFIER

#: Shown in place of an identifier that does not match `_IDENTIFIER`.
WITHHELD: Final = "`(identifier withheld: not a plain identifier)`"

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MARKDOWN_SPECIALS = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>])")
_AUTOLINK = re.compile(r"(?i)(://|www\.)")


def _plain(value: str, limit: int = _MAX_PLAIN) -> str:
    """An untrusted string, as inert markdown text on one line.

    Order matters: redaction runs on the original text so a token split by
    escaping is still found; HTML entities are escaped before markdown so the
    `&` of an entity is not escaped twice.
    """
    text = _redact_text(value)
    text = _CONTROL.sub(" ", text).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("|", _PIPE)
    text = _MARKDOWN_SPECIALS.sub(r"\\\1", text)
    # Backslashes do not stop GitHub linking a mention, an issue reference or a
    # bare URL. A zero-width space does.
    text = text.replace("@", "@" + _ZWSP)
    return _AUTOLINK.sub(lambda m: m.group(1)[0] + _ZWSP + m.group(1)[1:], text)


def _code(value: str, limit: int = 200) -> str:
    """An identifier or path in a code span, where markdown and HTML are literal.

    A backtick would close the span, so it is removed rather than escaped
    (escapes do not work inside code spans). A `|` is replaced too: GitHub
    splits a table row on `|` before it parses code spans, so a pipe inside a
    span still ends the cell and lets the rest render as live markdown.
    """
    text = _redact_text(value)
    text = _CONTROL.sub(" ", text).replace("\r", " ").replace("\n", " ").replace("`", "'")
    text = text.replace("|", _PIPE)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return f"`{text.strip() or '?'}`"


def _ident(value: str) -> str:
    """A model-chosen identifier in a code span, or `WITHHELD`."""
    return _code(value, 80) if _IDENTIFIER.match(value) else WITHHELD


@dataclass(frozen=True)
class PullRequestContext:
    """The stored artifacts the description is built from, as they were stored.

    Payloads are passed raw, exactly as the pipeline persisted them, and are
    parsed here. `None` means the artifact does not exist; it is rendered as
    missing, never as passed.
    """

    #: The selected workflows, from the analysis filtered by the selection.
    workflows: Sequence[Workflow] = ()
    #: The `SECURITY_REVIEW` artifact payload.
    security_review: Mapping[str, Any] | None = None
    #: The `VALIDATION` artifact payload.
    validation: Mapping[str, Any] | None = None
    #: The tool-plan artifact's `types_not_checked`: tool name → sentences.
    types_not_checked: Mapping[str, Sequence[str]] = field(default_factory=dict)


def default_title(plan: ToolPlan) -> str:
    count = len(plan.tools)
    return f"Add {count} WebMCP tool{'' if count == 1 else 's'} (generated by MCPForge)"


# -- stored artifacts, read pessimistically ----------------------------------


def _verdict(payload: Mapping[str, Any] | None) -> tuple[GateVerdict | None, str]:
    """The gate's verdict, or `None` and a reason it cannot be shown as passed."""
    if payload is None:
        return None, "No security review is recorded for this patch."
    if payload.get("completed") is not True:
        return None, "The security review did not complete, so no verdict exists."
    try:
        return GateVerdict.model_validate(payload.get("verdict")), ""
    except ValidationError:
        return None, "The stored security review could not be read."


def _report(payload: Mapping[str, Any] | None) -> tuple[ValidationReport | None, str]:
    if payload is None:
        return None, "No validation is recorded for this patch."
    if payload.get("completed") is not True:
        return None, "Validation did not complete, so no check produced a verdict."
    try:
        return ValidationReport.model_validate(payload.get("report")), ""
    except ValidationError:
        return None, "The stored validation report could not be read."


def _validated(payload: Mapping[str, Any] | None, report: ValidationReport | None) -> bool:
    """The pipeline's own pass condition: every check passed and every tool check ran."""
    return (
        report is not None
        and payload is not None
        and payload.get("validated") is True
        and report.passed
    )


def _trust(payload: Mapping[str, Any] | None) -> TrustLevel:
    """The recorded trust level. Absent or unrecognised is development isolation."""
    raw = payload.get("trust_level") if payload is not None else None
    try:
        return TrustLevel(raw) if isinstance(raw, str) else TrustLevel.DEVELOPMENT_ISOLATION
    except ValueError:
        return TrustLevel.DEVELOPMENT_ISOLATION


# -- sections ------------------------------------------------------------------


def _workflows_section(plan: ToolPlan, workflows: Sequence[Workflow]) -> list[str]:
    lines = ["## Workflows mapped", ""]
    by_id = {w.id: w for w in workflows}
    ids = [w.id for w in workflows] + sorted({t.workflow_id for t in plan.tools} - set(by_id))
    if not ids:
        return [*lines, "No workflow selection is recorded for this run."]
    # Labelled by id. The workflow's name and description were written by the
    # model and are not rendered.
    lines += ["| Workflow | Risk | Tools |", "| --- | --- | --- |"]
    for wid in ids:
        tools = [t.name for t in plan.tools if t.workflow_id == wid]
        known = by_id.get(wid)
        risk = known.risk.value if known else "not in the recorded selection"
        rendered = ", ".join(_ident(n) for n in tools) or "no tool"
        lines.append(f"| {_ident(wid)} | {risk} | {rendered} |")
    return lines


def _security_section(payload: Mapping[str, Any] | None) -> list[str]:
    lines = ["## Security review", ""]
    verdict, why = _verdict(payload)
    if verdict is None:
        return [*lines, f"**Result: NOT PASSED.** {why}"]

    result = "PASSED" if verdict.passed else "FAILED"
    lines.append(
        f"**Result: {result}**, decided by MCPForge's deterministic gate "
        "(policy engine plus the reviewer's findings). The model's opinion is advisory."
    )
    if verdict.overridden:
        lines += [
            "",
            "**The model reviewer's verdict was overridden.** It reported a pass; the "
            "deterministic gate did not agree, and the gate's verdict is the one that counts.",
        ]
    elif verdict.agent_said_pass != verdict.passed:
        lines += [
            "",
            f"The model reviewer's advisory verdict ({_word(verdict.agent_said_pass)}) "
            f"differed from the gate's ({_word(verdict.passed)}). "
            "The gate's verdict is the one that counts.",
        ]

    lines += ["", "| Severity | Findings |", "| --- | --- |"]
    for severity in sorted(Severity, key=lambda s: -s.rank):
        count = sum(1 for f in verdict.findings if f.severity is severity)
        lines.append(f"| {severity.value} | {count} |")

    if verdict.findings:
        # Rule id, severity and source only. Summaries and recommendations may
        # be model text and are read in MCPForge, not here.
        lines += [""]
        for finding in sorted(verdict.findings, key=lambda f: -f.severity.rank):
            source = "policy engine" if finding.deterministic else "model reviewer"
            lines.append(f"- **{finding.severity.value}** {_ident(finding.rule)} ({source})")
        lines += ["", "Each finding's detail is in the run's security review in MCPForge."]
    return lines


def _validation_section(
    payload: Mapping[str, Any] | None, report: ValidationReport | None, why: str
) -> list[str]:
    lines = ["## Validation", ""]
    if report is None:
        return [*lines, f"**Result: NOT PASSED.** {why}"]

    passed = [c for c in report.checks if c.passed]
    failed = [c for c in report.checks if not c.passed]
    result = "PASSED" if _validated(payload, report) else "NOT PASSED"
    lines += [
        f"**Result: {result}.** {len(passed)} passed, {len(failed)} failed, "
        f"{len(report.skipped)} skipped — each from a command's exit code in the "
        "validation workspace.",
        "",
    ]
    unexecuted = payload.get("unexecuted_tool_checks") if payload else None
    if isinstance(unexecuted, list) and unexecuted:
        lines += [
            f"{len(unexecuted)} tool check(s) did not run, so the integration is not "
            "validated: " + ", ".join(_code(str(c)) for c in unexecuted),
            "",
        ]
    for check in passed:
        lines.append(f"- passed {_code(check.check_id)}")
    for check in failed:
        how = "timed out" if check.evidence.timed_out else f"exit {check.evidence.exit_code}"
        lines.append(f"- **failed** {_code(check.check_id)} ({how})")
    for skipped in report.skipped:
        lines.append(f"- skipped {_code(skipped.check_id)}: {_plain(skipped.reason)}")
    return lines


def _readiness_section(report: ValidationReport | None) -> list[str]:
    lines = ["## Agent Readiness Score", ""]
    if report is None:
        return [*lines, "No readiness score is recorded: validation produced no report."]
    score = report.score
    lines += [
        f"**{score.total}/{score.max_total}**, arithmetic over executed checks only. "
        "A component with no evidence scores zero.",
        "",
        "| Component | Points | Why |",
        "| --- | --- | --- |",
    ]
    for row in score.components:
        lines.append(f"| {row.label} | {row.points}/{row.weight} | {_plain(row.detail)} |")
    if score.ungraded:
        lines += [
            "",
            "Recorded but not scored: " + ", ".join(_code(c.check_id) for c in score.ungraded),
        ]
    return lines


def _warnings_section(context: PullRequestContext, report: ValidationReport | None) -> list[str]:
    lines = ["## Warnings", ""]
    verdict, _ = _verdict(context.security_review)
    if verdict is None or not verdict.passed:
        lines.append("- The security review is not recorded as passed for this patch.")
    if not _validated(context.validation, report):
        lines.append("- Validation is not recorded as passed for this patch.")

    trust = _trust(context.validation)
    if trust is TrustLevel.DEVELOPMENT_ISOLATION:
        lines.append(
            "- Checks ran under `DEVELOPMENT_ISOLATION`: an isolated development "
            "workspace, not a hardware-attested environment. No attestation is claimed."
        )
    else:
        lines.append(f"- Checks ran under `{trust.value}`, as recorded for this run.")

    if report is not None and report.skipped:
        lines.append(
            f"- {len(report.skipped)} check(s) were skipped and produced no evidence; "
            "see Validation."
        )

    unchecked = {k: v for k, v in context.types_not_checked.items() if v}
    if unchecked:
        lines.append(
            "- Some types could not be checked when the tools were bound to your "
            "functions. Your own typecheck still decides:"
        )
        for tool, sentences in sorted(unchecked.items()):
            for sentence in sentences:
                lines.append(f"  - {_ident(tool)}: {_plain(sentence)}")

    if len(lines) == 2:
        lines.append("None recorded.")
    return lines


_EXAMPLES: Final[dict[str, str]] = {
    "string": '"example"',
    "number": "1",
    "integer": "1",
    "boolean": "true",
    "array": "[]",
    "object": "{}",
}


def _example_input(parameters: Sequence[ToolParameter]) -> str | None:
    """A minimal valid input, or `None` if a parameter name is not a plain identifier."""
    required = [p for p in parameters if p.required]
    if any(not _IDENTIFIER.match(p.name) for p in required):
        return None
    fields = [f'"{p.name}": {_EXAMPLES.get(p.json_type, "null")}' for p in required]
    return "{" + ", ".join(fields) + "}"


def _commands_run(report: ValidationReport | None) -> list[str]:
    """The application scripts MCPForge actually ran, from the stored argv."""
    if report is None:
        return []
    return [
        " ".join(c.evidence.argv) for c in report.checks if c.evidence.argv[:2] == ("npm", "run")
    ]


def _testing_section(plan: ToolPlan, report: ValidationReport | None, branch: str) -> list[str]:
    ran = _commands_run(report)
    commands = ran or ["npm run typecheck", "npm run build"]
    source = (
        "the commands MCPForge ran during validation"
        if ran
        else "suggested; MCPForge has no record of running your scripts"
    )
    lines = [
        "## How to test",
        "",
        f"1. Check out {_code(branch)} and install dependencies (`npm ci`).",
        f"2. Run these ({source}):",
    ]
    lines += [f"   - {_code(c)}" for c in commands]
    lines += [
        "3. Start the app (`npm run dev`) and open it in a WebMCP-capable browser. "
        "Confirm each tool below is registered on `document.modelContext`, then "
        "invoke it from a WebMCP client:",
    ]
    for tool in plan.tools:
        expect = (
            "it must return an approval request id and change nothing until a person approves"
            if tool.approval_required
            else f"it should return what {_function(tool.maps_to_function)} returns"
        )
        example = _example_input(tool.parameters)
        given = _code(example) if example is not None else WITHHELD
        lines.append(f"   - {_ident(tool.name)} with {given}: {expect}.")
    lines.append(
        "4. Call a tool that takes inputs with a wrong type; it must be rejected, "
        "not passed through."
    )
    return lines


def _function(name: str) -> str:
    """The user's function a tool calls, as `name()`, or `WITHHELD`."""
    return _code(name + "()", 80) if _IDENTIFIER.match(name) else WITHHELD


def _tools_section(plan: ToolPlan) -> list[str]:
    lines = [
        "## Tools",
        "",
        "| Tool | Risk | Calls your function | Human approval |",
        "| --- | --- | --- | --- |",
    ]
    for tool in plan.tools:
        approval = "Required" if tool.approval_required else "Not required"
        lines.append(
            f"| {_ident(tool.name)} | {tool.risk.value} | "
            f"{_function(tool.maps_to_function)} | {approval} |"
        )
    lines += [
        "",
        "Every handler calls the function named above. No business logic is "
        "duplicated, so changing that function changes the tool. Each tool's "
        "description, as an agent will read it, is in the generated registration "
        "code in this pull request.",
    ]
    gated: list[ToolPlanEntry] = [t for t in plan.tools if t.approval_required]
    if gated:
        lines += [
            "",
            "### What stops for you",
            "",
            f"{len(gated)} tool(s) change state and do not act on their own. They record "
            "an approval request and return its id, so an AI agent cannot complete them "
            "without a person deciding:",
            "",
        ]
        lines += [f"- {_ident(t.name)} ({t.risk.value})" for t in gated]
        lines += [
            "",
            "They post to `/api/webmcp/approvals`, which is yours to implement. Until you "
            "do, those tools refuse rather than act.",
        ]
    return lines


def _files_section(patch: GeneratedPatch) -> list[str]:
    lines = [
        "## Files",
        "",
        f"{len(patch.files)} file(s) added, +{patch.total_added}/-{patch.total_removed}.",
        "",
    ]
    # A file's rationale can name a tool by its model-written title, so it is
    # not rendered: the path, the kind of change and the tool it serves are.
    for change in patch.files:
        serves = f", for {_ident(change.affected_tool)}" if change.affected_tool else ""
        lines.append(f"- {_code(change.path)} ({change.kind.value}{serves})")
    return lines


def untrusted_text(
    plan: ToolPlan, patch: GeneratedPatch, context: PullRequestContext | None = None
) -> str:
    """Every non-literal string the body renders, **before** escaping.

    Escaping inserts backslashes and zero-width spaces, which can split a
    credential so the outbound scan no longer matches it (`xoxb-…` becomes
    `xoxb\\-…`). The writer therefore scans this text as well as the body, so a
    credential the redactor does not know is still refused rather than leaked.
    """
    ctx = context or PullRequestContext()
    parts: list[str] = []
    for tool in plan.tools:
        parts += [tool.name, tool.description, tool.maps_to_function, tool.workflow_id]
    parts += [w.id + "\n" + w.name for w in ctx.workflows]
    parts += [c.path + "\n" + c.rationale for c in patch.files]
    for sentences in ctx.types_not_checked.values():
        parts += [str(s) for s in sentences]
    verdict, _ = _verdict(ctx.security_review)
    if verdict is not None:
        for finding in verdict.findings:
            parts += [finding.rule, finding.summary, finding.recommendation]
    report, _ = _report(ctx.validation)
    if report is not None:
        parts += [s.reason for s in report.skipped]
    return "\n".join(parts)


def _cap(lines: list[str]) -> str:
    """Join, and cut at a line boundary with a note if GitHub would refuse it."""
    body = "\n".join(lines)
    if len(body) <= GITHUB_BODY_LIMIT:
        return body
    budget = GITHUB_BODY_LIMIT - len(TRUNCATION_NOTE)
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept)[:budget] + TRUNCATION_NOTE


def describe_patch(
    plan: ToolPlan,
    patch: GeneratedPatch,
    *,
    branch: str,
    base_commit: str,
    context: PullRequestContext | None = None,
) -> str:
    """The pull-request body.

    Sections are ordered by what a reviewer must not miss, so that if the body
    is ever truncated it loses the file list, not the security result. Without
    a context every artifact-backed section says it has no record.
    """
    ctx = context or PullRequestContext()
    report, why = _report(ctx.validation)
    sections = [
        _security_section(ctx.security_review),
        _warnings_section(ctx, report),
        _workflows_section(plan, ctx.workflows),
        _tools_section(plan),
        _validation_section(ctx.validation, report, why),
        _readiness_section(report),
        _testing_section(plan, report, branch),
        _files_section(patch),
    ]
    lines = [
        "This pull request was generated by MCPForge and reviewed by a human before it was opened.",
    ]
    for section in sections:
        lines += ["", *section]
    lines += [
        "",
        "---",
        "",
        f"Branch {_code(branch)} from {_code(base_commit[:12])}. Merge it as you normally would.",
    ]
    return _cap(lines)
