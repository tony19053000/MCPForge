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

    if tool.approval_required:
        behaviour = (
            '  it("refuses to act without approval", async () => {\n'
            f"    // {tool.risk.value}: it must request approval rather than calling\n"
            f"    // {tool.source.symbol}(). If this passes without an approval being\n"
            "    // requested, the gate has been removed.\n"
            f"    const result = await {tool.handler_name}({{ {valid_arguments(tool)} }});\n"
            '    expect("awaitingApproval" in result).toBe(true);\n'
            "  });"
        )
    else:
        behaviour = (
            '  it("returns a result for valid input", async () => {\n'
            f"    const result = await {tool.handler_name}({{ {valid_arguments(tool)} }});\n"
            "    expect(result.ok).toBe(true);\n"
            "  });"
        )

    invalid = ""
    if first is not None:
        args = valid_arguments(tool, override=(first.name, _WRONG[first.json_type]))
        invalid = (
            "\n\n"
            f'  it("rejects the wrong type for {first.name}", async () => {{\n'
            f"    const result = await {tool.handler_name}({{ {args} }});\n"
            '    expect(result).toMatchObject({ ok: false, code: "invalid_input" });\n'
            "  });"
        )

    return (
        f"{header}\n"
        'import { describe, expect, it } from "vitest";\n\n'
        f'import {{ {tool.handler_name} }} from "@/webmcp/tools/{tool.handler_name}";\n\n'
        f"describe({as_ts_string(tool.name)}, () => {{\n"
        f"{behaviour}{invalid}\n"
        "});\n"
    )
