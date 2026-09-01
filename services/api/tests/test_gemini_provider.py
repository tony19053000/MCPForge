"""Gemini provider — F2-01 acceptance criteria.

The two rules that matter are tested against the REAL provider with a stubbed
SDK client, not against the fake, because they are properties of the real code:
model output is re-validated by us, and the model id comes from configuration.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from pydantic import BaseModel

from mcpforge.config import GeminiBackend, Settings
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.gemini.google_provider import GoogleGenAIProvider
from mcpforge.gemini.provider import (
    GeminiNotConfiguredError,
    GeminiProvider,
    GeminiSchemaError,
    GeminiTransportError,
    GenerationRequest,
    Message,
    TraceContext,
)

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


class Analysis(BaseModel):
    framework: str
    routes: list[str]
    confidence: float


TRACE = TraceContext(project_id="p1", run_id="r1", agent="analyst", step="detect_framework")


def request(text: str = "analyze this") -> GenerationRequest:
    return GenerationRequest(
        system_instruction="You are the Codebase Analyst.",
        messages=[Message(role="user", text=text)],
        trace=TRACE,
    )


# --------------------------------------------------------------------------
# Stub SDK client — only the transport is substituted.
# --------------------------------------------------------------------------


class _Response:
    def __init__(self, text: str | None) -> None:
        self.text = text


class _Models:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)


class _Aio:
    def __init__(self, models: _Models) -> None:
        self.models = models


class StubClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.models = _Models(outcomes)
        self.aio = _Aio(self.models)


def provider(outcomes: list[Any], **overrides: Any) -> GoogleGenAIProvider:
    settings = Settings(
        gemini_api_key="test-key",
        gemini_model=overrides.pop("model", "gemini-3.7-flash"),
        gemini_max_retries=overrides.pop("retries", 2),
        gemini_retry_base_delay_seconds=0.0,
        **overrides,
    )
    return GoogleGenAIProvider(settings, client=StubClient(outcomes))  # type: ignore[arg-type]


# --------------------------------------------------------------------------


async def test_structured_call_returns_a_validated_instance() -> None:
    p = provider(['{"framework":"nextjs","routes":["/","/book"],"confidence":0.92}'])
    result = await p.generate_structured(request(), Analysis)
    assert isinstance(result, Analysis)
    assert result.framework == "nextjs"
    assert result.routes == ["/", "/book"]


async def test_response_is_revalidated_by_us_not_trusted_from_the_sdk() -> None:
    """The core rule: a schema-violating response raises rather than passing through.

    `confidence` is a string and `routes` is missing. If we trusted the SDK's
    claim that the response matched the schema, this would leak an invalid
    object into the orchestrator.
    """
    bad = '{"framework":"nextjs","confidence":"very high"}'
    p = provider([bad, bad, bad], retries=0)
    with pytest.raises(GeminiSchemaError, match="schema validation"):
        await p.generate_structured(request(), Analysis)


async def test_a_partial_object_is_never_returned() -> None:
    p = provider(['{"framework":"nextjs"}'], retries=0)
    with pytest.raises(GeminiSchemaError):
        await p.generate_structured(request(), Analysis)


async def test_non_json_text_raises_a_schema_error() -> None:
    p = provider(["Sure! Here is the analysis you asked for."], retries=0)
    with pytest.raises(GeminiSchemaError, match="not JSON"):
        await p.generate_structured(request(), Analysis)


async def test_empty_response_raises() -> None:
    p = provider([None], retries=0)
    with pytest.raises(GeminiSchemaError, match="empty"):
        await p.generate_structured(request(), Analysis)


async def test_schema_failure_is_retried_then_succeeds() -> None:
    p = provider(
        ['{"framework":"nextjs"}', '{"framework":"nextjs","routes":[],"confidence":0.5}'],
        retries=1,
    )
    result = await p.generate_structured(request(), Analysis)
    assert result.confidence == 0.5


async def test_retryable_transport_error_is_retried() -> None:
    p = provider(
        [
            RuntimeError("503 service unavailable"),
            '{"framework":"vite","routes":[],"confidence":1}',
        ],
        retries=1,
    )
    assert (await p.generate_structured(request(), Analysis)).framework == "vite"


async def test_non_retryable_transport_error_fails_immediately() -> None:
    p = provider([RuntimeError("400 invalid argument")], retries=2)
    with pytest.raises(GeminiTransportError) as exc:
        await p.generate_structured(request(), Analysis)
    assert exc.value.retryable is False


async def test_retries_are_bounded() -> None:
    p = provider([RuntimeError("429 rate limited")] * 3, retries=2)
    with pytest.raises(GeminiTransportError):
        await p.generate_structured(request(), Analysis)


async def test_model_id_comes_from_configuration() -> None:
    p = provider(['{"framework":"x","routes":[],"confidence":0}'], model="gemini-9.9-flash")
    await p.generate_structured(request(), Analysis)
    assert p.model == "gemini-9.9-flash"


async def test_the_configured_model_is_what_is_actually_sent() -> None:
    client = StubClient(['{"framework":"x","routes":[],"confidence":0}'])
    settings = Settings(gemini_api_key="k", gemini_model="gemini-3.7-flash")
    p = GoogleGenAIProvider(settings, client=client)  # type: ignore[arg-type]
    await p.generate_structured(request(), Analysis)
    assert client.models.calls[0]["model"] == "gemini-3.7-flash"


def test_no_model_id_literal_appears_outside_configuration() -> None:
    """A model id in agent logic would silently pin us to one model."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "config.py":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "gemini-" in code and "gemini_model" not in code:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert not offenders, "Model id literal outside config:\n" + "\n".join(offenders)


async def test_unconfigured_provider_refuses_rather_than_pretending() -> None:
    settings = Settings(gemini_api_key=None)
    p = GoogleGenAIProvider(settings)
    assert p.configured is False
    with pytest.raises(GeminiNotConfiguredError, match="not configured"):
        await p.generate_structured(request(), Analysis)


async def test_streaming_yields_chunks() -> None:
    fake = FakeGeminiProvider(["I found seven workflows"])
    chunks = [c async for c in fake.stream_text(request())]
    assert "".join(chunks).strip() == "I found seven workflows"


def test_the_fake_satisfies_the_port() -> None:
    p: GeminiProvider = FakeGeminiProvider()
    assert isinstance(p, GeminiProvider)


async def test_the_fake_also_revalidates_so_tests_cannot_accept_bad_output() -> None:
    fake = FakeGeminiProvider([{"framework": "nextjs"}])
    with pytest.raises(GeminiSchemaError):
        await fake.generate_structured(request(), Analysis)


async def test_trace_context_is_recorded_on_every_call() -> None:
    fake = FakeGeminiProvider([{"framework": "n", "routes": [], "confidence": 0}])
    await fake.generate_structured(request(), Analysis)
    assert fake.calls[0].trace.agent == "analyst"
    assert fake.calls[0].trace.step == "detect_framework"


def test_vertex_backend_needs_no_api_key() -> None:
    """ADC-based access is fully supported, so no key is required anywhere."""
    settings = Settings(
        gemini_backend=GeminiBackend.VERTEX,
        gemini_api_key=None,
        google_cloud_project="launchforge-tee",
    )
    assert settings.gemini_configured is True


def test_vertex_backend_without_a_project_is_unconfigured() -> None:
    settings = Settings(gemini_backend=GeminiBackend.VERTEX, google_cloud_project=None)
    assert settings.gemini_configured is False
