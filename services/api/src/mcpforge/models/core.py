"""Core domain models — 02_ARCHITECTURE.md §6.

These are the objects the whole product turns on. The rules that matter here:

- Run state is an enum with a legal transition table, checked in code.
- An `Approval` carries the hash of the artifact it covers, so regenerating an
  artifact automatically invalidates any prior approval of it.
- Nothing here holds repository file content.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def artifact_hash(payload: Any) -> str:
    """Stable hash of an artifact, used to bind an approval to what was approved."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


class RunState(StrEnum):
    PROJECT_CREATED = "PROJECT_CREATED"
    REPOSITORY_CONNECTED = "REPOSITORY_CONNECTED"
    ANALYSIS_PENDING = "ANALYSIS_PENDING"
    ANALYSIS_RUNNING = "ANALYSIS_RUNNING"
    ANALYSIS_COMPLETE = "ANALYSIS_COMPLETE"
    WORKFLOW_SELECTION_PENDING = "WORKFLOW_SELECTION_PENDING"
    WORKFLOWS_SELECTED = "WORKFLOWS_SELECTED"
    TOOL_PLAN_RUNNING = "TOOL_PLAN_RUNNING"
    TOOL_PLAN_READY = "TOOL_PLAN_READY"
    TOOL_PLAN_APPROVAL_PENDING = "TOOL_PLAN_APPROVAL_PENDING"
    TOOL_PLAN_APPROVED = "TOOL_PLAN_APPROVED"
    GENERATION_RUNNING = "GENERATION_RUNNING"
    PATCH_READY = "PATCH_READY"
    SECURITY_REVIEW_RUNNING = "SECURITY_REVIEW_RUNNING"
    SECURITY_REVIEW_FAILED = "SECURITY_REVIEW_FAILED"
    SECURITY_REVIEW_PASSED = "SECURITY_REVIEW_PASSED"
    PATCH_APPROVAL_PENDING = "PATCH_APPROVAL_PENDING"
    PATCH_APPROVED = "PATCH_APPROVED"
    VALIDATION_RUNNING = "VALIDATION_RUNNING"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    PR_APPROVAL_PENDING = "PR_APPROVAL_PENDING"
    PR_APPROVED = "PR_APPROVED"
    PR_CREATING = "PR_CREATING"
    PR_CREATED = "PR_CREATED"
    COMPLETE = "COMPLETE"


class AccessMode(StrEnum):
    """Repository access. Read-only until a human widens it."""

    READ_ONLY = "READ_ONLY"
    WRITE_PR = "WRITE_PR"


class ApprovalGate(StrEnum):
    """Named gates. Approving one never approves another."""

    TOOL_PLAN = "TOOL_PLAN"
    PATCH = "PATCH"
    PULL_REQUEST = "PULL_REQUEST"
    ACCESS_ELEVATION = "ACCESS_ELEVATION"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Origin(StrEnum):
    """Who initiated an action. Set server-side, never supplied by the caller."""

    HUMAN = "HUMAN"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


class Project(BaseModel):
    id: str = Field(default_factory=lambda: new_id("proj"))
    owner_uid: str
    name: str
    # A demo project has no bound repository and can never reach a write path.
    repository_id: str | None = None
    repository_full_name: str | None = None
    base_branch: str | None = None
    access_mode: AccessMode = AccessMode.READ_ONLY
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_demo(self) -> bool:
        return self.repository_id is None


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sess"))
    project_id: str
    owner_uid: str
    title: str = "New session"
    state: RunState = RunState.PROJECT_CREATED
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Turn(BaseModel):
    """One message in a conversation. Never holds repository file content."""

    id: str = Field(default_factory=lambda: new_id("turn"))
    session_id: str
    role: str  # "user" | "assistant"
    text: str
    origin: Origin = Origin.HUMAN
    created_at: datetime = Field(default_factory=utcnow)


class RunEvent(BaseModel):
    """Activity timeline entry.

    Carries task-level summaries only — counts, paths, exit codes. Raw model
    reasoning is never placed on an event (04_FRONTEND_SPEC.md §3).
    """

    id: str = Field(default_factory=lambda: new_id("evt"))
    session_id: str
    kind: str  # step.started | step.completed | approval.requested | ...
    label: str
    detail: dict[str, Any] = Field(default_factory=dict)
    origin: Origin = Origin.SYSTEM
    created_at: datetime = Field(default_factory=utcnow)


class Approval(BaseModel):
    """A recorded human decision.

    This is the only thing that opens a gate. Model output never is.
    """

    id: str = Field(default_factory=lambda: new_id("appr"))
    project_id: str
    session_id: str
    gate: ApprovalGate
    # Binds the decision to exactly what was shown. If the artifact is
    # regenerated its hash changes, and this approval no longer applies.
    artifact_hash: str
    summary: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    actor_uid: str | None = None

    def covers(self, current_hash: str) -> bool:
        """True only if this is an approval, for this exact artifact."""
        return self.status is ApprovalStatus.APPROVED and self.artifact_hash == current_hash
