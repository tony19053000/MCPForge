"""WebMCP tool contracts — ticket F5-01, 02_ARCHITECTURE.md §10.

The typed representation of a tool as it will exist in the browser. Validated
here, before a line of code is generated, because a schema that is wrong at
generation time produces code that compiles and then misbehaves.

The API shape follows the W3C Web Machine Learning CG draft:
`document.modelContext.registerTool(tool, { signal })`. There is no
`unregisterTool`; teardown is the abort signal. See `apps/web/src/webmcp/`.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from mcpforge.models.analysis import Evidence, RiskClass
from mcpforge.models.toolplan import (
    FORBIDDEN_PARAMETER_NAMES,
    GENERATED_IDENTIFIERS,
    TOOL_NAME_PATTERN,
)

#: A generated identifier must be a plain TypeScript identifier. Anything else
#: is either a mistake or an injection attempt into the generated file.
TS_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

#: Import specifiers we will write into a generated file.
#:
#: Generated files always live under `src/webmcp/`, so the `@/` alias is the only
#: form needed. Relative `../` specifiers are refused: they are a way to reach
#: outside the source tree and we never require one.
SAFE_IMPORT = re.compile(r"^@/[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-.]+)*$")


class ToolInputProperty(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    json_type: str
    description: str = Field(min_length=1, max_length=200)
    required: bool = True

    @field_validator("name")
    @classmethod
    def _usable_as_an_identifier(cls, v: str) -> str:
        if not TS_IDENTIFIER.match(v):
            raise ValueError(f"{v!r} is not a valid TypeScript identifier")
        if v.lower() in FORBIDDEN_PARAMETER_NAMES:
            raise ValueError(f"{v!r} grants authority the application never gave the caller")
        if v.lower() in GENERATED_IDENTIFIERS:
            raise ValueError(
                f"{v!r} collides with an identifier the generator declares; "
                "the generated file would not compile"
            )
        return v

    @field_validator("json_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        allowed = {"string", "number", "integer", "boolean", "array", "object"}
        if v not in allowed:
            raise ValueError(f"json_type must be one of {sorted(allowed)}")
        return v

    @property
    def ts_type(self) -> str:
        return {
            "string": "string",
            "number": "number",
            "integer": "number",
            "boolean": "boolean",
            "array": "unknown[]",
            "object": "Record<string, unknown>",
        }[self.json_type]


class CallStyle(StrEnum):
    """How the target function takes its arguments.

    The generator cannot guess this. `searchRooms(params: {...})` takes one
    object; `checkAvailability(roomId, checkIn, checkOut)` takes three
    positionals. Emitting the wrong shape produces code that typechecks in
    isolation and fails against the real application, which is the failure mode
    this whole phase exists to avoid.
    """

    POSITIONAL = "positional"
    OBJECT = "object"


class SourceBinding(BaseModel):
    """The existing function a generated tool must call.

    02_ARCHITECTURE.md §10: "Prefer existing services/functions. Do NOT duplicate
    core application logic." This is what makes that checkable — the generator
    must import *this* symbol from *this* module, and call it the way it is
    actually declared.
    """

    module: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=1, max_length=80)
    is_async: bool = False
    call_style: CallStyle = CallStyle.POSITIONAL
    #: The function's own parameter names, taken from the index. For OBJECT
    #: style these are the keys of the single argument.
    parameters: list[str] = Field(default_factory=list)

    @field_validator("parameters")
    @classmethod
    def _identifiers(cls, v: list[str]) -> list[str]:
        for name in v:
            if not TS_IDENTIFIER.match(name):
                raise ValueError(f"{name!r} is not a valid parameter name")
        return v

    @field_validator("module")
    @classmethod
    def _safe_specifier(cls, v: str) -> str:
        if ".." in v:
            raise ValueError(f"{v!r} climbs out of the source tree")
        if not SAFE_IMPORT.match(v):
            raise ValueError(
                f"{v!r} is not a safe import specifier; generated files use the @/ alias"
            )
        if v.lower() in GENERATED_IDENTIFIERS:
            raise ValueError(
                f"{v!r} collides with an identifier the generator declares; "
                "the generated file would not compile"
            )
        return v

    @field_validator("symbol")
    @classmethod
    def _usable_as_an_identifier(cls, v: str) -> str:
        if not TS_IDENTIFIER.match(v):
            raise ValueError(f"{v!r} is not a valid TypeScript identifier")
        return v


class WebMCPTool(BaseModel):
    """One tool, ready to generate. Everything here has been validated."""

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    title: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=300)
    inputs: list[ToolInputProperty] = Field(default_factory=list)
    output_description: str = Field(min_length=1, max_length=300)
    risk: RiskClass
    approval_required: bool
    source: SourceBinding
    evidence: list[Evidence] = Field(min_length=1)

    @model_validator(mode="after")
    def _inputs_cover_the_target_signature(self) -> WebMCPTool:
        """Every parameter the target function needs must have a tool input.

        Checked on the contract rather than discovered when the generated file
        fails to compile. A tool that cannot supply an argument is not a tool.
        """
        if not self.source.parameters:
            return self
        supplied = {p.name for p in self.inputs}
        missing = [p for p in self.source.parameters if p not in supplied]
        if missing:
            raise ValueError(
                f"tool {self.name!r} calls {self.source.symbol}"
                f"({', '.join(self.source.parameters)}) but has no input for {missing}"
            )
        return self

    @model_validator(mode="after")
    def _approval_matches_risk(self) -> WebMCPTool:
        """A contract that says DESTRUCTIVE and "no approval" cannot exist.

        The reconciliation upstream already sets this, but a tool contract is
        what the generator reads, so the invariant is enforced where it is used
        rather than only where it is derived.
        """
        if self.risk.requires_approval and not self.approval_required:
            raise ValueError(f"tool {self.name!r} is {self.risk.value} and must require approval")
        return self

    @property
    def handler_name(self) -> str:
        """camelCase handler name derived from the snake_case tool name."""
        head, *rest = self.name.split("_")
        return head + "".join(part.capitalize() for part in rest)

    @property
    def type_name(self) -> str:
        """PascalCase, from the tool name rather than from the handler name.

        Capitalising the whole camelCase handler gives `Cancelreservation`.
        """
        return "".join(part.capitalize() for part in self.name.split("_"))

    @property
    def import_alias(self) -> str:
        """The name the developer's function is imported under.

        A generated handler derived from `cancel_reservation` is
        `cancelReservation`, which is exactly what the source symbol is often
        called. Importing it unaliased would shadow the handler — TypeScript
        would reject the redeclaration, and a subtler version of the same
        mistake is an infinite recursion. Always aliased, so the collision
        cannot occur regardless of naming.
        """
        return f"{self.source.symbol}Impl"

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                p.name: {"type": p.json_type, "description": p.description} for p in self.inputs
            },
            "required": [p.name for p in self.inputs if p.required],
            "additionalProperties": False,
        }

    # Rendering lives in `generation/`, not here: every line of a generated file
    # must pass through the escaping boundary, and a model has written these
    # descriptions.


class WebMCPToolset(BaseModel):
    """Every tool for one project, plus what they collectively need."""

    tools: list[WebMCPTool] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_are_unique(self) -> WebMCPToolset:
        names = [t.name for t in self.tools]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate tool names: {duplicates}")
        return self

    @property
    def requires_approval(self) -> list[WebMCPTool]:
        return [t for t in self.tools if t.approval_required]

    def modules(self) -> list[str]:
        return sorted({t.source.module for t in self.tools})
