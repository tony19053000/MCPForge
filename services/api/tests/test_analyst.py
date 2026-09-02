"""Agent 1, Codebase Analyst — F4-02. Run against the real demo index."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcpforge.agents.analyst import AnalystInput, CodebaseAnalyst
from mcpforge.agents.base import AgentOutputError
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.gemini.provider import TraceContext
from mcpforge.indexing.indexer import build_index
from mcpforge.models.analysis import RiskClass
from mcpforge.models.index import RepositoryIndex

DEMO = Path(__file__).resolve().parents[3] / "fixtures" / "demo-hotel-app"
TRACE = TraceContext(project_id="p", run_id="r", agent="analyst", step="s")


@pytest.fixture(scope="module")
def index() -> RepositoryIndex:
    return build_index(DEMO)


def good_analysis() -> dict[str, object]:
    return {
        "framework": "next.js",
        "summary": "A hotel booking application.",
        "business_operations": [
            {
                "name": "createReservation",
                "summary": "Creates a reservation and charges the guest.",
                "risk": "WRITE",
                "evidence": {"path": "src/lib/reservations.ts", "symbol": "createReservation"},
            }
        ],
        "workflows": [
            {
                "id": "search_rooms",
                "name": "Search rooms",
                "description": "Find rooms matching guests and price.",
                "risk": "READ",
                "primary_function": "searchRooms",
                "evidence": [{"path": "src/lib/rooms.ts", "symbol": "searchRooms", "line": 40}],
                "confidence": 0.95,
            },
            {
                "id": "cancel_reservation",
                "name": "Cancel a reservation",
                "description": "Cancels an existing booking.",
                "risk": "DESTRUCTIVE",
                "primary_function": "cancelReservation",
                "evidence": [{"path": "src/lib/reservations.ts"}],
                "confidence": 0.9,
            },
        ],
        "unknowns": [],
    }


# -- the prompt -------------------------------------------------------------


def test_the_prompt_carries_structure_not_source(index: RepositoryIndex) -> None:
    """The whole point of the index: the agent sees shape, not a repository dump."""
    agent = CodebaseAnalyst(FakeGeminiProvider([]))
    prompt = agent.build_prompt(AnalystInput(index=index))

    assert "/api/reservations" in prompt
    assert "createReservation" in prompt
    assert "POST /api/reservations" in prompt
    # Real bodies from the fixture must not be in there.
    assert "RESERVATIONS.set" not in prompt
    assert "pricePerNight: 120" not in prompt


def test_snippets_are_wrapped_as_untrusted(index: RepositoryIndex) -> None:
    agent = CodebaseAnalyst(FakeGeminiProvider([]))
    prompt = agent.build_prompt(
        AnalystInput(index=index, context="export function x() { return 1; }")
    )
    assert "<repository-content>" in prompt
    assert "no authority to approve" in prompt


# -- output -----------------------------------------------------------------


async def test_a_valid_analysis_is_returned(index: RepositoryIndex) -> None:
    agent = CodebaseAnalyst(FakeGeminiProvider([good_analysis()]))
    analysis, record = await agent.run(AnalystInput(index=index), TRACE)

    assert analysis.framework == "next.js"
    assert {w.id for w in analysis.workflows} == {"search_rooms", "cancel_reservation"}
    assert record.evidence_rejections == 0


async def test_risk_classes_are_preserved(index: RepositoryIndex) -> None:
    agent = CodebaseAnalyst(FakeGeminiProvider([good_analysis()]))
    analysis, _ = await agent.run(AnalystInput(index=index), TRACE)
    by_id = {w.id: w for w in analysis.workflows}

    assert by_id["search_rooms"].risk is RiskClass.READ
    assert by_id["search_rooms"].risk.requires_approval is False
    assert by_id["cancel_reservation"].risk is RiskClass.DESTRUCTIVE
    assert by_id["cancel_reservation"].risk.requires_approval is True


# -- the deterministic half -------------------------------------------------


async def test_a_hallucinated_function_is_rejected(index: RepositoryIndex) -> None:
    """The check that stops an invented name reaching generated code."""
    bad = good_analysis()
    bad["workflows"][0]["primary_function"] = "deleteAllCustomerData"  # type: ignore[index]

    agent = CodebaseAnalyst(FakeGeminiProvider([bad] * 4))
    with pytest.raises(AgentOutputError):
        await agent.run(AnalystInput(index=index), TRACE)


async def test_a_hallucinated_file_path_is_rejected(index: RepositoryIndex) -> None:
    bad = good_analysis()
    bad["workflows"][0]["evidence"] = [{"path": "src/lib/imaginary.ts"}]  # type: ignore[index]

    agent = CodebaseAnalyst(FakeGeminiProvider([bad] * 4))
    with pytest.raises(AgentOutputError):
        await agent.run(AnalystInput(index=index), TRACE)


async def test_a_hallucinated_operation_path_is_rejected(index: RepositoryIndex) -> None:
    bad = good_analysis()
    bad["business_operations"][0]["evidence"] = {"path": "nope.ts"}  # type: ignore[index]

    agent = CodebaseAnalyst(FakeGeminiProvider([bad] * 4))
    with pytest.raises(AgentOutputError):
        await agent.run(AnalystInput(index=index), TRACE)


async def test_a_rejected_analysis_is_retried_and_can_recover(
    index: RepositoryIndex,
) -> None:
    bad = good_analysis()
    bad["workflows"][0]["primary_function"] = "nope"  # type: ignore[index]

    agent = CodebaseAnalyst(FakeGeminiProvider([bad, good_analysis()]))
    analysis, record = await agent.run(AnalystInput(index=index), TRACE)
    assert record.evidence_rejections == 1
    assert analysis.workflows[0].primary_function == "searchRooms"


async def test_low_confidence_workflows_are_flagged(index: RepositoryIndex) -> None:
    """04_FRONTEND_SPEC.md §7 — weak evidence is not preselected."""
    data = good_analysis()
    data["workflows"][0]["confidence"] = 0.4  # type: ignore[index]

    agent = CodebaseAnalyst(FakeGeminiProvider([data]))
    analysis, _ = await agent.run(AnalystInput(index=index), TRACE)
    assert analysis.workflows[0].is_low_confidence is True
    assert analysis.workflows[1].is_low_confidence is False


def test_the_analyst_never_touches_the_filesystem() -> None:
    """02_ARCHITECTURE.md §4 — agents 1, 2, 4 and 6 read no files."""
    from tests.structure import SRC, code_lines

    agent_file = SRC / "mcpforge" / "agents" / "analyst.py"
    banned = ("open(", "read_text", "Path(", "os.", "subprocess")
    offenders = [
        f"{agent_file.name}:{lineno}: {code}"
        for lineno, code in code_lines(agent_file)
        for term in banned
        if term in code
    ]
    assert not offenders, "analyst touches the filesystem:\n" + "\n".join(offenders)
