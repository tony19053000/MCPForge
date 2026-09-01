"""The Gemini provider port — 02_ARCHITECTURE.md §3.1.

Every agent talks to Gemini through this interface and nothing else. Two rules
are enforced here rather than left to each caller:

1. Structured output is re-validated by Pydantic on our side. A model response
   is never trusted because the SDK said it matched a schema.
2. The model id comes from configuration. No literal appears in agent logic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@dataclass(frozen=True)
class TraceContext:
    """Identifies the work a call belongs to, for the activity timeline.

    Carries no prompt text and no repository content — only identifiers.
    """

    project_id: str
    run_id: str
    agent: str
    step: str
    attempt: int = 1


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "model"
    text: str


@dataclass(frozen=True)
class GenerationRequest:
    system_instruction: str
    messages: Sequence[Message]
    trace: TraceContext
    temperature: float | None = None
    max_output_tokens: int | None = None
    labels: dict[str, str] = field(default_factory=dict)


class GeminiError(Exception):
    """Base for provider failures. Always typed; never a bare exception."""


class GeminiNotConfiguredError(GeminiError):
    """No usable credentials. Distinct from a failed call, so the product can
    report 'unconfigured' honestly rather than implying the model refused."""


class GeminiTransportError(GeminiError):
    """The call did not complete: network, timeout, rate limit, server error."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class GeminiSchemaError(GeminiError):
    """The model returned something that is not valid against the requested
    schema. Raised rather than returning a partial object."""

    def __init__(self, message: str, *, raw: str | None = None) -> None:
        super().__init__(message)
        # Retained for debugging only. Never streamed to a client.
        self.raw = raw


@runtime_checkable
class GeminiProvider(Protocol):
    """The only way MCPForge reaches a model."""

    @property
    def configured(self) -> bool: ...

    @property
    def model(self) -> str: ...

    async def generate_structured(
        self, request: GenerationRequest, schema: type[SchemaT]
    ) -> SchemaT: ...

    def stream_text(self, request: GenerationRequest) -> AsyncIterator[str]: ...
