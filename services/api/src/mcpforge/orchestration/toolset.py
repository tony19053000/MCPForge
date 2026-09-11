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
from mcpforge.models.index import (
    FileNode,
    RepositoryIndex,
    Symbol,
    SymbolKind,
    TsType,
    TypedField,
    TypedParameter,
)
from mcpforge.models.toolplan import ToolParameter, ToolPlan, ToolPlanEntry
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


#: The TypeScript kind each declared JSON type narrows to in generated code
#: (`generation/nextjs.py`, `_TYPE_GUARDS`). `integer` is a `number` at runtime.
JSON_TO_TS: dict[str, TsType] = {
    "string": TsType.STRING,
    "number": TsType.NUMBER,
    "integer": TsType.NUMBER,
    "boolean": TsType.BOOLEAN,
    "array": TsType.ARRAY,
    "object": TsType.OBJECT,
}


def _describe(slot: TypedParameter | TypedField) -> str:
    return f"{slot.name}{'?' if slot.optional else ''}: {slot.ts_type.value}"


def _check_slot(
    tool: ToolPlanEntry,
    where: str,
    slot: TypedParameter | TypedField,
    prop: ToolParameter,
    problems: list[str],
    unchecked: list[str],
) -> None:
    """One tool input against the parameter or field it fills.

    Refuses only what the declaration's syntax settles. An `UNKNOWN` type is
    never refused on type, and never counted as checked: it is recorded.
    """
    if slot.ts_type is TsType.UNKNOWN:
        unchecked.append(
            f"{where} has a type MCPForge cannot read from syntax alone, so the "
            f"{prop.json_type} input {prop.name!r} was not type-checked against it"
        )
        return
    if slot.ts_type is TsType.ANY:
        return  # `any` / `unknown` accept every value, optional or not.

    expected = JSON_TO_TS[prop.json_type]
    if slot.ts_type is not expected:
        problems.append(
            f"tool {tool.name!r} declares {prop.name!r} as {prop.json_type}, but {where} "
            f"is declared {slot.ts_type.value}"
        )
        return
    if slot.ts_type in (TsType.ARRAY, TsType.OBJECT):
        detail = "element type" if slot.ts_type is TsType.ARRAY else "shape"
        unchecked.append(
            f"{where} is an {slot.ts_type.value}; its kind matches {prop.name!r}, "
            f"but its {detail} was not checked"
        )
    if not slot.optional and not prop.required:
        problems.append(
            f"tool {tool.name!r} marks {prop.name!r} optional, but {where} is required; "
            "the generated call would pass undefined where the declaration forbids it"
        )


def _aligned_signature(symbol: Symbol) -> list[TypedParameter] | None:
    """The typed signature, only if it lines up with the parameter names."""
    if symbol.params and [p.name for p in symbol.signature] == symbol.params:
        return list(symbol.signature)
    return None


def _check_object_call(
    tool: ToolPlanEntry, symbol: Symbol, param: TypedParameter | None, unchecked: list[str]
) -> list[str]:
    """Every input a field, every required field supplied, every type compatible."""
    problems: list[str] = []
    if param is None or param.fields is None:
        unchecked.append(
            f"{symbol.name}'s object parameter has no inline shape MCPForge can read, so "
            "field names, required fields and types were not checked"
        )
        return problems

    where = f"{symbol.name}({param.name}: {{...}})"
    fields = {f.name: f for f in param.fields}
    known = ", ".join(_describe(f) for f in param.fields) or "no fields"
    for prop in tool.parameters:
        field = fields.get(prop.name)
        if field is None:
            problems.append(
                f"tool {tool.name!r} declares {prop.name!r}, which {where} does not have "
                f"(it has {known})"
            )
            continue
        _check_slot(tool, f"{where}.{field.name}", field, prop, problems, unchecked)

    supplied = {p.name for p in tool.parameters}
    missing = [f.name for f in param.fields if not f.optional and f.name not in supplied]
    if missing:
        problems.append(
            f"tool {tool.name!r} omits required field(s) {missing} of {where} (it has {known})"
        )
    return problems


def _binding(tool: ToolPlanEntry, file: FileNode, symbol: Symbol) -> SourceBinding:
    """Decide how the generated handler calls the function, from its declaration.

    - One parameter declared as an object → the handler passes one object whose
      keys are the tool's inputs. When the object's shape is inline, every
      input must be one of its fields, every required field must be supplied,
      and each declared JSON type must fit the field's type.
    - Otherwise positional. The tool's inputs must be exactly the function's
      parameters (by name), and are passed in the function's declared order —
      except that a single-parameter function called with a single input is
      bound by position, since there is nothing to reorder or confuse. Each
      input's JSON type must fit its parameter's type.

    Anything else is refused. Extra tool inputs, missing arguments or an
    incompatible type would make the approved schema a lie — and, for a tool
    that calls straight through, produce code the application's typecheck
    rejects (the first live F9-01 analysis run). A type the index could not
    classify is not refused; it is recorded in `SourceBinding.unchecked`.
    """
    inputs = [p.name for p in tool.parameters]
    params = symbol.params
    signature = _aligned_signature(symbol)
    unchecked: list[str] = []
    if params and signature is None:
        unchecked.append(
            f"the index recorded no parameter types for {symbol.name}, so no input's type "
            "was checked"
        )

    if len(params) == 1 and params[0] in symbol.object_params:
        if not inputs:
            raise ToolsetConversionError(
                f"tool {tool.name!r} takes no inputs, but {symbol.name} expects an object"
            )
        problems = _check_object_call(tool, symbol, signature[0] if signature else None, unchecked)
        if problems:
            raise ToolsetConversionError("; ".join(problems))
        return SourceBinding(
            module=module_specifier(file.path),
            symbol=symbol.name,
            is_async=symbol.is_async,
            call_style=CallStyle.OBJECT,
            parameters=inputs,
            unchecked=unchecked,
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

    if signature is not None:
        mismatches: list[str] = []
        by_name = {p.name: p for p in tool.parameters}
        if len(params) == 1 and len(inputs) == 1:
            pairs = [(signature[0], tool.parameters[0])]
        else:
            pairs = [(slot, by_name[slot.name]) for slot in signature]
        for slot, prop in pairs:
            _check_slot(tool, f"{symbol.name}({slot.name})", slot, prop, mismatches, unchecked)
        if mismatches:
            raise ToolsetConversionError("; ".join(mismatches))

    return SourceBinding(
        module=module_specifier(file.path),
        symbol=symbol.name,
        is_async=symbol.is_async,
        call_style=CallStyle.POSITIONAL,
        parameters=ordered,
        unchecked=unchecked,
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
