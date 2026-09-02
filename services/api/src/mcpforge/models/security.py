"""Security review output — 02_ARCHITECTURE.md §4, agent 4.

The agent's verdict is **advisory**. It is one input to a deterministic gate
that also runs our own policy checks, and an agent PASS can never clear a
violation found by code (03_SECURITY_ACCESS.md §7).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from mcpforge.models.analysis import Evidence


class Severity(StrEnum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"].index(self.value)

    @property
    def blocks(self) -> bool:
        return self.rank >= Severity.HIGH.rank


class Finding(BaseModel):
    rule: str = Field(min_length=1, max_length=80)
    severity: Severity
    summary: str = Field(min_length=1, max_length=300)
    recommendation: str = Field(min_length=1, max_length=400)
    evidence: Evidence | None = None
    #: Set for findings produced by our own policy engine rather than the model.
    deterministic: bool = False


class SecurityReport(BaseModel):
    """Agent 4's output. `advisory_pass` is what the *model* thinks."""

    advisory_pass: bool
    findings: list[Finding] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=600)

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity.blocks]


class GateVerdict(BaseModel):
    """What deterministic code decided. This is what the orchestrator reads.

    `passed` is computed from policy findings and the agent's findings together.
    The agent cannot set it.
    """

    passed: bool
    findings: list[Finding]
    agent_said_pass: bool
    overridden: bool = False
    reason: str = ""
