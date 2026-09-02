"""WebMCP tool plan — 02_ARCHITECTURE.md §4, 03_SECURITY_ACCESS.md §8.

What the Workflow Architect proposes and the developer approves. A tool is not
generated until a human has seen this and said yes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from mcpforge.models.analysis import Evidence, RiskClass

#: Tool names are snake_case verbs at the level of intent — `search_hotels`,
#: never `click_button`. Enforced, because the naming *is* the product value.
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{2,48}[a-z0-9]$"

#: Parameter names that hand an agent authority the application never granted.
FORBIDDEN_PARAMETER_NAMES = frozenset(
    {
        "sql",
        "query",
        "where",
        "filter_sql",
        "raw",
        "table",
        "collection",
        "database",
        "db",
        "path",
        "file",
        "filepath",
        "filename",
        "dir",
        "directory",
        "url",
        "endpoint",
        "host",
        "callback",
        "redirect",
        "user_id",
        "owner_id",
        "account_id",
        "tenant_id",
        "role",
        "roles",
        "scope",
        "scopes",
        "permission",
        "permissions",
        "admin",
        "is_admin",
        "token",
        "api_key",
        "password",
        "secret",
    }
)


class ToolParameter(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    json_type: str
    description: str = Field(min_length=1, max_length=200)
    required: bool = True

    @field_validator("json_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        allowed = {"string", "number", "integer", "boolean", "array", "object"}
        if v not in allowed:
            raise ValueError(f"json_type must be one of {sorted(allowed)}")
        return v


class ToolPlanEntry(BaseModel):
    """One proposed WebMCP tool."""

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    title: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=300)
    workflow_id: str = Field(min_length=1)
    #: The existing function this tool must call. Never reimplemented.
    maps_to_function: str = Field(min_length=1)
    parameters: list[ToolParameter] = Field(default_factory=list)
    output_description: str = Field(min_length=1, max_length=300)
    risk: RiskClass
    evidence: list[Evidence] = Field(min_length=1)
    #: Set by deterministic code, not by the model. See ToolPlan.reconcile_risk.
    approval_required: bool = False

    def input_schema(self) -> dict[str, Any]:
        """The JSON Schema a browser agent will see."""
        return {
            "type": "object",
            "properties": {
                p.name: {"type": p.json_type, "description": p.description} for p in self.parameters
            },
            "required": [p.name for p in self.parameters if p.required],
            "additionalProperties": False,
        }

    def forbidden_parameters(self) -> list[str]:
        return sorted(
            p.name for p in self.parameters if p.name.lower() in FORBIDDEN_PARAMETER_NAMES
        )


class ToolPlan(BaseModel):
    """Agent 2's output, before the deterministic risk reconciliation."""

    tools: list[ToolPlanEntry] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]
