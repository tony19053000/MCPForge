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

#: A value of the wrong type, for the rejection case.
_WRONG = {
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


def test_file(tool: WebMCPTool, header: str) -> str:
    first = tool.inputs[0] if tool.inputs else None
    args = valid_arguments(tool)

    if tool.approval_required:
        lines = [
            '  it("refuses to act without approval", async () => {',
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
            '  it("returns a result for valid input", async () => {',
            f"    const result = await {tool.handler_name}({{ {args} }});",
            "    expect(result.ok).toBe(true);",
            "  });",
        ]

    if first is not None:
        wrong = valid_arguments(tool, override=(first.name, _WRONG[first.json_type]))
        lines += [
            "",
            f'  it("rejects the wrong type for {first.name}", async () => {{',
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
