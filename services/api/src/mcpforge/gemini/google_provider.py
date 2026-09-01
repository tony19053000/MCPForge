"""Gemini over the official `google-genai` SDK.

Supports both authentication paths behind one interface:

- `api_key`  — a key from Google AI Studio.
- `vertex`   — Application Default Credentials against a GCP project, which
               needs no secret at all. Preferred where organization policy
               restricts key material, which is our situation
               (03_SECURITY_ACCESS.md §9).

Choosing between them is configuration, not a code change, and no caller knows
which is in use.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator

from google import genai
from google.genai import types
from pydantic import ValidationError

from mcpforge.config import GeminiBackend, Settings
from mcpforge.gemini.provider import (
    GeminiNotConfiguredError,
    GeminiSchemaError,
    GeminiTransportError,
    GenerationRequest,
    SchemaT,
)
from mcpforge.logging import get_logger

log = get_logger(__name__)

# Errors worth retrying. Anything else is a real failure and is raised at once.
_RETRYABLE_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "deadline",
    "timeout",
    "unavailable",
    "resource_exhausted",
)


def _is_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _RETRYABLE_MARKERS)


class GoogleGenAIProvider:
    """The real provider. Constructed once and shared."""

    def __init__(self, settings: Settings, *, client: genai.Client | None = None) -> None:
        self._settings = settings
        self._client = client or self._build_client(settings)

    @staticmethod
    def _build_client(settings: Settings) -> genai.Client | None:
        if not settings.gemini_configured:
            return None
        if settings.gemini_backend is GeminiBackend.VERTEX:
            # Credentials come from ADC. Nothing secret is passed here.
            return genai.Client(
                vertexai=True,
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
            )
        return genai.Client(api_key=settings.gemini_api_key)

    @property
    def configured(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return self._settings.gemini_model

    def _require_client(self) -> genai.Client:
        if self._client is None:
            raise GeminiNotConfiguredError(
                "Gemini is not configured: set GEMINI_API_KEY, or set "
                "GEMINI_BACKEND=vertex with GOOGLE_CLOUD_PROJECT and ADC."
            )
        return self._client

    @staticmethod
    def _contents(request: GenerationRequest) -> list[types.Content]:
        return [
            types.Content(role=m.role, parts=[types.Part.from_text(text=m.text)])
            for m in request.messages
        ]

    def _config(
        self, request: GenerationRequest, schema: type[SchemaT] | None
    ) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=request.system_instruction,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            response_mime_type="application/json" if schema else None,
            response_schema=schema,
            http_options=types.HttpOptions(timeout=self._settings.gemini_timeout_seconds * 1000),
        )

    async def generate_structured(
        self, request: GenerationRequest, schema: type[SchemaT]
    ) -> SchemaT:
        """Return a validated instance of `schema`, or raise.

        The SDK is asked for JSON matching the schema, and the result is then
        validated by us. A response the SDK accepted but Pydantic rejects is an
        error, not a partial success.
        """
        client = self._require_client()
        attempts = self._settings.gemini_max_retries + 1
        last_transport: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = await client.aio.models.generate_content(
                    model=self.model,
                    contents=self._contents(request),
                    config=self._config(request, schema),
                )
            except Exception as exc:
                if _is_retryable(exc) and attempt < attempts:
                    last_transport = exc
                    await self._backoff(attempt, request, exc)
                    continue
                raise GeminiTransportError(
                    f"Gemini call failed: {exc}", retryable=_is_retryable(exc)
                ) from exc

            raw = response.text
            if not raw:
                raise GeminiSchemaError("Gemini returned an empty response")

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GeminiSchemaError(
                    f"Gemini returned text that is not JSON: {exc}", raw=raw
                ) from exc

            try:
                return schema.model_validate(payload)
            except ValidationError as exc:
                # Never returned partially validated. A schema failure is worth
                # one retry, since it is often a transient formatting slip.
                if attempt < attempts:
                    log.warning(
                        "gemini.schema_retry",
                        agent=request.trace.agent,
                        step=request.trace.step,
                        attempt=attempt,
                        errors=len(exc.errors()),
                    )
                    continue
                raise GeminiSchemaError(
                    f"Gemini response failed schema validation: {exc}", raw=raw
                ) from exc

        raise GeminiTransportError(
            f"Gemini call failed after {attempts} attempts: {last_transport}", retryable=True
        )

    async def _backoff(self, attempt: int, request: GenerationRequest, exc: Exception) -> None:
        base = self._settings.gemini_retry_base_delay_seconds
        if base <= 0:
            return
        delay = min(base * 2**attempt, 8.0) * (0.5 + random.random() / 2)  # noqa: S311
        log.warning(
            "gemini.retry",
            agent=request.trace.agent,
            step=request.trace.step,
            attempt=attempt,
            delay_seconds=round(delay, 2),
            reason=type(exc).__name__,
        )
        await asyncio.sleep(delay)

    async def stream_text(self, request: GenerationRequest) -> AsyncIterator[str]:
        """Stream plain text. Used for conversation, never for structured state."""
        client = self._require_client()
        try:
            stream = await client.aio.models.generate_content_stream(
                model=self.model,
                contents=self._contents(request),
                config=self._config(request, None),
            )
            async for chunk in stream:
                if chunk.text:
                    yield chunk.text
        except Exception as exc:
            raise GeminiTransportError(
                f"Gemini stream failed: {exc}", retryable=_is_retryable(exc)
            ) from exc
