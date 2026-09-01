"""Live Gemini smoke check — run manually, never in CI.

Proves the configured backend actually reaches the model and that structured
output survives our own re-validation. Prints no key material.

    uv run python scripts/live_check.py
"""

from __future__ import annotations

import asyncio
import sys

from pydantic import BaseModel, Field

from mcpforge.config import GeminiBackend, get_settings
from mcpforge.gemini.google_provider import GoogleGenAIProvider
from mcpforge.gemini.provider import GenerationRequest, Message, TraceContext


class WorkflowGuess(BaseModel):
    """Deliberately strict, so a sloppy response fails validation rather than passing."""

    framework: str = Field(min_length=1)
    workflows: list[str] = Field(min_length=2, max_length=5)
    confidence: float = Field(ge=0.0, le=1.0)


async def main() -> int:
    settings = get_settings()

    print(f"backend : {settings.gemini_backend.value}")
    print(f"model   : {settings.gemini_model}")
    if settings.gemini_backend is GeminiBackend.VERTEX:
        print(f"project : {settings.google_cloud_project} ({settings.google_cloud_location})")
    else:
        key = settings.gemini_api_key
        print(f"api key : {'set, ' + str(len(key)) + ' chars' if key else 'MISSING'}")

    if not settings.gemini_configured:
        print("\nNot configured. Set GEMINI_API_KEY, or GEMINI_BACKEND=vertex with")
        print("GOOGLE_CLOUD_PROJECT and Application Default Credentials.")
        return 2

    provider = GoogleGenAIProvider(settings)

    request = GenerationRequest(
        system_instruction=(
            "You are the MCPForge Codebase Analyst. Given a description of a web "
            "application, identify its framework and the business workflows an AI "
            "agent could usefully perform. Respond only with the requested JSON."
        ),
        messages=[
            Message(
                role="user",
                text=(
                    "A Next.js App Router hotel site. It has a search page, a room "
                    "availability calendar, a reservation form that calls "
                    "createReservation(), and a cancellation flow."
                ),
            )
        ],
        trace=TraceContext(project_id="live-check", run_id="live-1", agent="analyst", step="smoke"),
    )

    print("\n--- structured call ---")
    result = await provider.generate_structured(request, WorkflowGuess)
    print(f"framework  : {result.framework}")
    print(f"workflows  : {', '.join(result.workflows)}")
    print(f"confidence : {result.confidence}")

    print("\n--- streaming call ---")
    stream_request = GenerationRequest(
        system_instruction="Answer in one short sentence.",
        messages=[Message(role="user", text="What does WebMCP let a website do?")],
        trace=TraceContext(
            project_id="live-check", run_id="live-1", agent="interaction", step="smoke"
        ),
    )
    async for chunk in provider.stream_text(stream_request):
        print(chunk, end="", flush=True)
    print("\n\nBoth calls succeeded, and the structured response passed our validation.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
