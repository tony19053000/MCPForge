"""Agent 2, Workflow Architect — F4-03.

The risk reconciliation is the security-relevant part: a model must not be able
to remove an approval gate by under-classifying a tool.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from mcpforge.agents.architect import (
    ArchitectInput,
    WorkflowArchitect,
    infer_risk_from_function,
    reconcile_risk,
)
from mcpforge.agents.base import AgentOutputError
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.gemini.provider import TraceContext
from mcpforge.indexing.indexer import build_index
from mcpforge.models.analysis import CodebaseAnalysis, RiskClass
from mcpforge.models.index import RepositoryIndex
from mcpforge.models.toolplan import ToolPlan

DEMO = Path(__file__).resolve().parents[3] / "fixtures" / "demo-hotel-app"
TRACE = TraceContext(project_id="p", run_id="r", agent="architect", step="s")


@pytest.fixture(scope="module")
def index() -> RepositoryIndex:
    return build_index(DEMO)


@pytest.fixture
def analysis() -> CodebaseAnalysis:
    return CodebaseAnalysis.model_validate(
        {
            "framework": "next.js",
            "summary": "hotel app",
            "workflows": [
                {
                    "id": "search_rooms",
                    "name": "Search rooms",
                    "description": "find rooms",
                    "risk": "READ",
                    "primary_function": "searchRooms",
                    "evidence": [{"path": "src/lib/rooms.ts"}],
                    "confidence": 0.9,
                },
                {
                    "id": "cancel",
                    "name": "Cancel a reservation",
                    "description": "cancel a booking",
                    "risk": "DESTRUCTIVE",
                    "primary_function": "cancelReservation",
                    "evidence": [{"path": "src/lib/reservations.ts"}],
                    "confidence": 0.9,
                },
            ],
        }
    )


def plan_data(**overrides: object) -> dict[str, object]:
    tool = {
        "name": "search_rooms",
        "title": "Search rooms",
        "description": "Find rooms matching guests and price.",
        "workflow_id": "search_rooms",
        "maps_to_function": "searchRooms",
        "parameters": [
            {"name": "guests", "json_type": "integer", "description": "Number of guests"},
            {
                "name": "max_price",
                "json_type": "number",
                "description": "Highest nightly price",
                "required": False,
            },
        ],
        "output_description": "Matching rooms.",
        "risk": "READ",
        "evidence": [{"path": "src/lib/rooms.ts"}],
    }
    tool.update(overrides)
    return {"tools": [tool], "notes": []}


# -- naming and schemas -----------------------------------------------------


async def test_a_valid_plan_is_returned(index: RepositoryIndex, analysis: CodebaseAnalysis) -> None:
    agent = WorkflowArchitect(FakeGeminiProvider([plan_data()]))
    plan, _ = await agent.run(
        ArchitectInput(index=index, analysis=analysis, selected_workflow_ids=["search_rooms"]),
        TRACE,
    )
    assert plan.tool_names() == ["search_rooms"]


@pytest.mark.parametrize("bad_name", ["clickButton", "Search_Rooms", "sr", "search rooms", "1tool"])
def test_ui_mechanic_and_malformed_names_are_rejected(bad_name: str) -> None:
    """The naming is the product value: intent-level, snake_case."""
    with pytest.raises(ValueError, match="tools"):
        ToolPlan.model_validate(plan_data(name=bad_name))


def test_the_generated_input_schema_is_valid_json_schema() -> None:
    plan = ToolPlan.model_validate(plan_data())
    schema = plan.tools[0].input_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["required"] == ["guests"]
    assert schema["additionalProperties"] is False


def test_the_schema_accepts_valid_input_and_rejects_invalid() -> None:
    schema = ToolPlan.model_validate(plan_data()).tools[0].input_schema()
    jsonschema.validate({"guests": 2}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"guests": "two"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"guests": 2, "sneaky": 1}, schema)


# -- verification -----------------------------------------------------------


async def test_a_tool_mapping_to_a_missing_function_is_rejected(
    index: RepositoryIndex, analysis: CodebaseAnalysis
) -> None:
    agent = WorkflowArchitect(
        FakeGeminiProvider([plan_data(maps_to_function="deleteEverything")] * 4)
    )
    with pytest.raises(AgentOutputError):
        await agent.run(
            ArchitectInput(index=index, analysis=analysis, selected_workflow_ids=["search_rooms"]),
            TRACE,
        )


async def test_a_tool_for_an_unselected_workflow_is_rejected(
    index: RepositoryIndex, analysis: CodebaseAnalysis
) -> None:
    """The developer chose which workflows agents may reach. Only those."""
    agent = WorkflowArchitect(
        FakeGeminiProvider(
            [plan_data(workflow_id="cancel", maps_to_function="cancelReservation")] * 4
        )
    )
    with pytest.raises(AgentOutputError):
        await agent.run(
            ArchitectInput(index=index, analysis=analysis, selected_workflow_ids=["search_rooms"]),
            TRACE,
        )


@pytest.mark.parametrize(
    "param", ["sql", "table", "path", "user_id", "role", "token", "is_admin", "redirect"]
)
async def test_parameters_that_grant_authority_are_rejected(
    index: RepositoryIndex, analysis: CodebaseAnalysis, param: str
) -> None:
    """03_SECURITY_ACCESS.md §8.2 — a tool must not take a parameter that
    selects a table, a path, a user or a permission."""
    data = plan_data(parameters=[{"name": param, "json_type": "string", "description": "x"}])
    agent = WorkflowArchitect(FakeGeminiProvider([data] * 4))
    with pytest.raises(AgentOutputError):
        await agent.run(
            ArchitectInput(index=index, analysis=analysis, selected_workflow_ids=["search_rooms"]),
            TRACE,
        )


async def test_duplicate_tool_names_are_rejected(
    index: RepositoryIndex, analysis: CodebaseAnalysis
) -> None:
    data = plan_data()
    data["tools"] = [data["tools"][0], dict(data["tools"][0])]  # type: ignore[index]
    agent = WorkflowArchitect(FakeGeminiProvider([data] * 4))
    with pytest.raises(AgentOutputError):
        await agent.run(
            ArchitectInput(index=index, analysis=analysis, selected_workflow_ids=["search_rooms"]),
            TRACE,
        )


async def test_an_empty_plan_is_rejected(
    index: RepositoryIndex, analysis: CodebaseAnalysis
) -> None:
    agent = WorkflowArchitect(FakeGeminiProvider([{"tools": [], "notes": []}] * 4))
    with pytest.raises(AgentOutputError):
        await agent.run(
            ArchitectInput(index=index, analysis=analysis, selected_workflow_ids=["search_rooms"]),
            TRACE,
        )


# -- risk reconciliation, the security-relevant part ------------------------


@pytest.mark.parametrize(
    ("function", "expected"),
    [
        ("searchRooms", RiskClass.READ),
        ("listReservations", RiskClass.READ),
        ("checkAvailability", RiskClass.READ),
        ("getRoom", RiskClass.READ),
        ("createReservation", RiskClass.WRITE),
        ("updateBooking", RiskClass.WRITE),
        ("bookRoom", RiskClass.WRITE),
        ("cancelReservation", RiskClass.DESTRUCTIVE),
        ("deleteAccount", RiskClass.DESTRUCTIVE),
        ("refundPayment", RiskClass.DESTRUCTIVE),
        ("purgeRecords", RiskClass.DESTRUCTIVE),
    ],
)
def test_risk_is_inferred_from_the_function_name(function: str, expected: RiskClass) -> None:
    assert infer_risk_from_function(function) is expected


def test_an_under_classified_destructive_tool_is_escalated() -> None:
    """The heart of it. A model claiming READ for cancelReservation must not be
    able to remove the approval gate by saying so."""
    plan = ToolPlan.model_validate(
        plan_data(
            name="cancel_reservation",
            maps_to_function="cancelReservation",
            workflow_id="cancel",
            risk="READ",
        )
    )
    reconciled, discrepancies = reconcile_risk(plan)

    assert reconciled.tools[0].risk is RiskClass.DESTRUCTIVE
    assert reconciled.tools[0].approval_required is True
    assert len(discrepancies) == 1
    assert discrepancies[0].proposed is RiskClass.READ
    assert discrepancies[0].enforced is RiskClass.DESTRUCTIVE
    assert "stricter" in discrepancies[0].reason


def test_an_under_classified_write_tool_is_escalated() -> None:
    plan = ToolPlan.model_validate(
        plan_data(name="create_booking", maps_to_function="createReservation", risk="READ")
    )
    reconciled, discrepancies = reconcile_risk(plan)
    assert reconciled.tools[0].risk is RiskClass.WRITE
    assert reconciled.tools[0].approval_required is True
    assert discrepancies


def test_an_over_classified_tool_keeps_the_stricter_class() -> None:
    """Only ever raises. A model being cautious costs one approval click."""
    plan = ToolPlan.model_validate(plan_data(maps_to_function="searchRooms", risk="DESTRUCTIVE"))
    reconciled, discrepancies = reconcile_risk(plan)
    assert reconciled.tools[0].risk is RiskClass.DESTRUCTIVE
    assert reconciled.tools[0].approval_required is True
    assert discrepancies == []


def test_an_agreed_read_tool_needs_no_approval() -> None:
    plan = ToolPlan.model_validate(plan_data())
    reconciled, discrepancies = reconcile_risk(plan)
    assert reconciled.tools[0].risk is RiskClass.READ
    assert reconciled.tools[0].approval_required is False
    assert discrepancies == []


def test_approval_required_is_derived_not_taken_from_the_model() -> None:
    """A model asserting approval_required=False on a destructive tool changes
    nothing: the field is computed."""
    data = plan_data(
        name="cancel_reservation",
        maps_to_function="cancelReservation",
        workflow_id="cancel",
        risk="DESTRUCTIVE",
    )
    data["tools"][0]["approval_required"] = False  # type: ignore[index]

    reconciled, _ = reconcile_risk(ToolPlan.model_validate(data))
    assert reconciled.tools[0].approval_required is True


def test_reconciliation_is_stable_when_applied_twice() -> None:
    plan = ToolPlan.model_validate(plan_data(maps_to_function="cancelReservation", risk="READ"))
    once, _ = reconcile_risk(plan)
    twice, discrepancies = reconcile_risk(once)
    assert twice.tools[0].risk is RiskClass.DESTRUCTIVE
    assert discrepancies == []


def test_the_plan_serializes_for_an_approval_hash() -> None:
    """An approval binds to the hash of what was shown, so the plan must have a
    stable serialization."""
    from mcpforge.models.core import artifact_hash

    plan, _ = reconcile_risk(ToolPlan.model_validate(plan_data()))
    first = artifact_hash(json.loads(plan.model_dump_json()))
    second = artifact_hash(json.loads(plan.model_dump_json()))
    assert first == second
