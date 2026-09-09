"""Generated tests — one per tool, F5-02.

The developer inherits these with the integration. They cover the properties
worth breaking loudly: input validation rejects the wrong type, and a gated tool
refuses to act rather than calling through.

Written for Vitest, which a Next.js project most likely already has. It is an
ordinary test file, meant to be adapted rather than treated as generated output
that must not be touched.
"""

from __future__ import annotations

from mcpforge.generation.escaping import as_ts_string
from mcpforge.models.webmcp import WebMCPTool

#: A plausible valid value for each declared type.
_SAMPLES = {
    "string": '"example"',
    "number": "1",
    "integer": "1",
    "boolean": "true",
    "array": "[]",
    "object": "{}",
}

#: A value of the wrong type, for the rejection case. Public because F8-04's
#: validation harness builds the same invalid call and must use the same
#: mapping — two tables of "what is the wrong type for a number" is how one of
#: them stops being wrong.
WRONG_TYPED_VALUES = {
    "string": "123",
    "number": '"not-a-number"',
    "integer": '"not-a-number"',
    "boolean": '"yes"',
    "array": '"not-an-array"',
    "object": '"not-an-object"',
}


def valid_arguments(tool: WebMCPTool, *, override: tuple[str, str] | None = None) -> str:
    parts: list[str] = []
    for prop in tool.inputs:
        if not prop.required and (override is None or override[0] != prop.name):
            continue
        value = override[1] if override and override[0] == prop.name else _SAMPLES[prop.json_type]
        parts.append(f"{prop.name}: {value}")
    return ", ".join(parts)


#: The generated test names, defined once because two things read them: this
#: module writes them into the generated file, and F8-04's validator selects
#: check results by them. A literal in either place is a second copy of the
#: same rule, and this ticket's predecessors lost review rounds to exactly that
#: — `test_every_targeted_test_name_is_one_the_generator_actually_emits` fails
#: if these drift from what `test_file` emits. That test name is the one that
#: exists; an earlier version of this comment cited a test that was never
#: written, which is a false trust claim of exactly the kind this phase's
#: review record keeps returning to.
AUTHORIZATION_TEST_NAME = "refuses to act without approval"
EXECUTION_TEST_NAME = "returns a result for valid input"


def rejection_test_name(tool: WebMCPTool) -> str | None:
    """The rejection test's name for `tool`, or **None** when it has no inputs.

    `test_file` emits this case only when the tool declares at least one input,
    and `WebMCPTool.inputs` defaults to an empty list, so a zero-input tool is
    legal and reachable.

    `None`, never `""`. The empty string was worse than useless: callers guard
    with `is not None`, so `""` passed the guard, and vitest treats an empty
    `-t` as matching *everything*. A zero-input tool therefore scored the full
    ERROR_HANDLING weight from the *execution* test's result, for a check the
    generator never emitted — a fabricated component score, and precisely the
    "no evidence contributes zero" criterion this ticket is built on.
    `test_a_tool_with_no_inputs_has_no_rejection_check` fails if a falsy string
    comes back again.
    """
    if not tool.inputs:
        return None
    return f"rejects the wrong type for {tool.inputs[0].name}"


def test_file(tool: WebMCPTool, header: str) -> str:
    first = tool.inputs[0] if tool.inputs else None
    args = valid_arguments(tool)

    if tool.approval_required:
        lines = [
            f'  it("{AUTHORIZATION_TEST_NAME}", async () => {{',
            f"    // {tool.risk.value}: it must request approval rather than calling",
            f"    // {tool.source.symbol}(). If this passes without an approval being",
            "    // requested, the gate has been removed.",
            "    //",
            "    // The approval endpoint is yours to implement. It is stubbed here so",
            "    // this test passes on first run rather than failing for a reason that",
            "    // is not about this tool.",
            '    vi.stubGlobal("fetch", vi.fn(async () =>',
            '      Response.json({ approvalId: "test-approval" }),',
            "    ));",
            f"    const result = await {tool.handler_name}({{ {args} }});",
            '    expect("awaitingApproval" in result).toBe(true);',
            "  });",
        ]
    else:
        lines = [
            f'  it("{EXECUTION_TEST_NAME}", async () => {{',
            f"    const result = await {tool.handler_name}({{ {args} }});",
            "    expect(result.ok).toBe(true);",
            "  });",
        ]

    if first is not None:
        wrong = valid_arguments(tool, override=(first.name, WRONG_TYPED_VALUES[first.json_type]))
        lines += [
            "",
            f'  it("{rejection_test_name(tool) or ""}", async () => {{',
            f"    const result = await {tool.handler_name}({{ {wrong} }});",
            '    expect(result).toMatchObject({ ok: false, code: "invalid_input" });',
            "  });",
        ]

    imports = "describe, expect, it" + (", vi" if tool.approval_required else "")
    behaviour = "\n".join(lines)

    return (
        f"{header}\n"
        f'import {{ {imports} }} from "vitest";\n\n'
        f'import {{ {tool.handler_name} }} from "@/webmcp/tools/{tool.handler_name}";\n\n'
        f"describe({as_ts_string(tool.name)}, () => {{\n"
        f"{behaviour}\n"
        "});\n"
    )
