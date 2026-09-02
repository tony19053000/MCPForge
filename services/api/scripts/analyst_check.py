"""Live check: index the demo app and run the real Codebase Analyst on it.

uv run python scripts/analyst_check.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcpforge.agents.analyst import AnalystInput, CodebaseAnalyst
from mcpforge.config import get_settings
from mcpforge.gemini.google_provider import GoogleGenAIProvider
from mcpforge.gemini.provider import TraceContext
from mcpforge.indexing.indexer import build_index
from mcpforge.indexing.retrieval import ContextRetriever, RetrievalRequest
from mcpforge.models.index import FileKind

DEMO = Path(__file__).resolve().parents[3] / "fixtures" / "demo-hotel-app"


async def main() -> int:
    settings = get_settings()
    if not settings.gemini_configured:
        print("Gemini is not configured.")
        return 2

    index = build_index(DEMO)
    print(
        f"indexed  : {len(index.files)} files, {index.framework.name} "
        f"{index.framework.version} ({index.framework.router} router)"
    )

    retriever = ContextRetriever(index, DEMO)
    context = retriever.retrieve(
        RetrievalRequest(preferred_kinds=[FileKind.SERVICE], token_budget=6000)
    )
    print(
        f"context  : {len(context.snippets)} snippets, ~{context.total_tokens} tokens "
        f"of a {context.budget} budget"
    )

    agent = CodebaseAnalyst(GoogleGenAIProvider(settings))
    analysis, record = await agent.run(
        AnalystInput(index=index, context=context.render()),
        TraceContext(project_id="live", run_id="analyst-check", agent="analyst", step="live"),
    )

    print(
        f"\nattempts : {record.attempts}  schema retries: {record.schema_retries}  "
        f"evidence rejections: {record.evidence_rejections}"
    )
    print(f"framework: {analysis.framework}")
    print(f"summary  : {analysis.summary}\n")

    print("WORKFLOWS")
    for workflow in analysis.workflows:
        flag = "  (low confidence)" if workflow.is_low_confidence else ""
        approval = "approval required" if workflow.risk.requires_approval else "no approval"
        print(f"  {workflow.name}")
        print(f"    risk       : {workflow.risk.value} — {approval}")
        print(f"    calls      : {workflow.primary_function}()")
        print(f"    evidence   : {', '.join(e.path for e in workflow.evidence)}")
        print(f"    confidence : {workflow.confidence}{flag}")

    if analysis.unknowns:
        print("\nUNKNOWNS (stated rather than guessed at)")
        for unknown in analysis.unknowns:
            print(f"  - {unknown}")

    print(
        "\nEvery claim above resolved against the index, or the agent would have been "
        "made to try again."
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
