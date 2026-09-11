"""From an approved tool plan to a generatable toolset — F9-01.

The Workflow Architect's `ToolPlan` says *which* function each tool calls and
what it takes. The generator needs more than that: the import specifier, whether
the function is async, and whether it takes one object or positional arguments.
None of that is asked of a model. It is read from the deterministic index, and a
plan that cannot be bound to a real declaration is refused here — **before** a
human is asked to approve it — rather than producing a patch that cannot compile.

Nothing here renames or reshapes what the plan says. A binding that would need
the plan changed to fit the code is an error, because the developer approves the
plan's schema, and a converter that quietly edited it would generate something
other than what was approved.
"""

from __future__ import annotations

from mcpforge.models.analysis import RiskClass
from mcpforge.models.index import FileNode, RepositoryIndex, Symbol, SymbolKind
from mcpforge.models.toolplan import ToolPlan, ToolPlanEntry
from mcpforge.models.webmcp import (
    CallStyle,
    SourceBinding,
    ToolInputProperty,
    WebMCPTool,
    WebMCPToolset,
)

#: Where a generated import may resolve from. The generator only ever writes the
#: `@/` alias (`models/webmcp.py`, `SAFE_IMPORT`), which a Next.js app maps to
#: `src/`, so a target function must live there.
SOURCE_ROOT = "src/"
SOURCE_SUFFIXES = (".ts", ".tsx")


class ToolsetConversionError(Exception):
    """The plan cannot be bound to the application's real code."""


def module_specifier(path: str) -> str:
    """`src/lib/rooms.ts` -> `@/lib/rooms`. Refuses anything outside `src/`."""
    if not path.startswith(SOURCE_ROOT) or not path.endswith(SOURCE_SUFFIXES):
        raise ToolsetConversionError(
            f"{path} is not a TypeScript module under {SOURCE_ROOT}, so a generated "
            "tool has no `@/` import that reaches it"
        )
    stem = path[len(SOURCE_ROOT) :].rsplit(".", 1)[0]
    return f"@/{stem}"


def _target(index: RepositoryIndex, tool: ToolPlanEntry) -> tuple[FileNode, Symbol]:
    found = index.find_symbol(tool.maps_to_function)
    if found is None:
        raise ToolsetConversionError(
            f"tool {tool.name!r} maps to {tool.maps_to_function!r}, which is not in the index"
        )
    file, symbol = found
    if symbol.kind is not SymbolKind.FUNCTION:
        raise ToolsetConversionError(
            f"tool {tool.name!r} maps to {symbol.name!r}, which is a {symbol.kind.value}, "
            "not a function"
        )
    if not symbol.exported:
        raise ToolsetConversionError(
            f"tool {tool.name!r} maps to {symbol.name!r}, which {file.path} does not export"
        )
    return file, symbol


def _binding(tool: ToolPlanEntry, file: FileNode, symbol: Symbol) -> SourceBinding:
    """Decide how the generated handler calls the function, from its declaration.

    - One parameter declared as an object → the handler passes one object whose
      keys are the tool's inputs.
    - Otherwise positional. The tool's inputs must be exactly the function's
      parameters (by name), and are passed in the function's declared order —
      except that a single-parameter function called with a single input is
      bound by position, since there is nothing to reorder or confuse.

    Anything else is refused. Extra tool inputs a positional call would drop,
    or missing arguments, would make the approved schema a lie.
    """
    inputs = [p.name for p in tool.parameters]
    params = symbol.params

    if len(params) == 1 and params[0] in symbol.object_params:
        if not inputs:
            raise ToolsetConversionError(
                f"tool {tool.name!r} takes no inputs, but {symbol.name} expects an object"
            )
        return SourceBinding(
            module=module_specifier(file.path),
            symbol=symbol.name,
            is_async=symbol.is_async,
            call_style=CallStyle.OBJECT,
            parameters=inputs,
        )

    if symbol.object_params:
        raise ToolsetConversionError(
            f"{symbol.name}({', '.join(params)}) mixes object and positional parameters; "
            "MCPForge does not guess how to call it"
        )

    if len(params) == 1 and len(inputs) == 1:
        ordered = inputs
    elif sorted(inputs) == sorted(params):
        ordered = list(params)
    else:
        raise ToolsetConversionError(
            f"tool {tool.name!r} takes ({', '.join(inputs) or 'nothing'}) but "
            f"{symbol.name} is declared as ({', '.join(params) or 'nothing'}); the plan "
            "would have to change to call it, and MCPForge does not change an approved plan"
        )

    return SourceBinding(
        module=module_specifier(file.path),
        symbol=symbol.name,
        is_async=symbol.is_async,
        call_style=CallStyle.POSITIONAL,
        parameters=ordered,
    )


def toolset_from_plan(plan: ToolPlan, index: RepositoryIndex) -> WebMCPToolset:
    """The one conversion from a reconciled plan to what the generator reads.

    The plan must already have been through `reconcile_risk`; its
    `approval_required` is carried across unchanged and `WebMCPTool` re-checks
    that it agrees with the risk class.
    """
    if not plan.tools:
        raise ToolsetConversionError("the plan contains no tools")

    tools: list[WebMCPTool] = []
    for entry in plan.tools:
        file, symbol = _target(index, entry)
        try:
            tools.append(
                WebMCPTool(
                    name=entry.name,
                    title=entry.title,
                    description=entry.description,
                    inputs=[
                        ToolInputProperty(
                            name=p.name,
                            json_type=p.json_type,
                            description=p.description,
                            required=p.required,
                        )
                        for p in entry.parameters
                    ],
                    output_description=entry.output_description,
                    risk=RiskClass(entry.risk),
                    approval_required=entry.approval_required,
                    source=_binding(entry, file, symbol),
                    evidence=entry.evidence,
                )
            )
        except ValueError as exc:
            # Pydantic's ValidationError is a ValueError. The contract model
            # refused the tool — an identifier the generator cannot emit, or a
            # risk class without its approval — so the plan is not generatable.
            raise ToolsetConversionError(f"tool {entry.name!r} is not generatable: {exc}") from exc

    try:
        return WebMCPToolset(tools=tools)
    except ValueError as exc:
        raise ToolsetConversionError(f"the toolset is not generatable: {exc}") from exc
