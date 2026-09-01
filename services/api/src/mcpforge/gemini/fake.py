"""A scripted provider for tests.

It exercises the same contract as the real one — including re-validation, so a
test cannot accidentally accept output the real provider would reject. It is
never selected by configuration and cannot be reached from the application.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable

from pydantic import ValidationError

from mcpforge.gemini.provider import (
    GeminiSchemaError,
    GeminiTransportError,
    GenerationRequest,
    SchemaT,
)


class FakeGeminiProvider:
    """Replays queued responses. A response may be a str, dict, or an Exception."""

    def __init__(
        self,
        responses: Iterable[str | dict[str, object] | Exception] = (),
        *,
        model: str = "fake-model",
        configured: bool = True,
    ) -> None:
        self._queue = list(responses)
        self._model = model
        self._configured = configured
        self.calls: list[GenerationRequest] = []

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def model(self) -> str:
        return self._model

    def _next(self) -> str | dict[str, object] | Exception:
        if not self._queue:
            raise AssertionError("FakeGeminiProvider ran out of scripted responses")
        return self._queue.pop(0)

    async def generate_structured(
        self, request: GenerationRequest, schema: type[SchemaT]
    ) -> SchemaT:
        self.calls.append(request)
        item = self._next()
        if isinstance(item, Exception):
            raise item

        raw = item if isinstance(item, str) else json.dumps(item)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GeminiSchemaError(f"not JSON: {exc}", raw=raw) from exc
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            raise GeminiSchemaError(f"schema validation failed: {exc}", raw=raw) from exc

    async def stream_text(self, request: GenerationRequest) -> AsyncIterator[str]:
        self.calls.append(request)
        item = self._next()
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, str):
            raise GeminiTransportError("stream responses must be text", retryable=False)
        for word in item.split(" "):
            yield word + " "
