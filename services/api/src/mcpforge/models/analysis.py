"""Agent output schemas — 02_ARCHITECTURE.md §4.

Every claim an agent makes carries evidence: a file path, and where possible a
symbol and a line. That is what makes `verify` able to reject a hallucination
deterministically instead of carrying it forward into generated code.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class RiskClass(StrEnum):
    """03_SECURITY_ACCESS.md §8.1."""

    READ = "READ"
    WRITE = "WRITE"
    DESTRUCTIVE = "DESTRUCTIVE"

    @property
    def requires_approval(self) -> bool:
        return self is not RiskClass.READ

    @property
    def rank(self) -> int:
        return {"READ": 0, "WRITE": 1, "DESTRUCTIVE": 2}[self.value]


class Evidence(BaseModel):
    """Where a claim comes from. Checked against the index, never trusted."""

    path: str = Field(min_length=1)
    symbol: str | None = None
    line: int | None = None


class BusinessOperation(BaseModel):
    """A function that does something meaningful to application state."""

    name: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=300)
    risk: RiskClass
    evidence: Evidence


class Workflow(BaseModel):
    """A user-facing task, joined across the files that implement it.

    This is the unit a developer selects, and the unit a WebMCP tool is designed
    for. A workflow with weak evidence is marked low confidence and is not
    preselected in the UI (04_FRONTEND_SPEC.md §7).
    """

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    risk: RiskClass
    #: The function a generated tool should call. Must exist in the index.
    primary_function: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < 0.6


class CodebaseAnalysis(BaseModel):
    """Agent 1's output."""

    framework: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=800)
    business_operations: list[BusinessOperation] = Field(default_factory=list)
    workflows: list[Workflow] = Field(default_factory=list)
    #: Anything the agent could not determine. Stated, not guessed at.
    unknowns: list[str] = Field(default_factory=list)
