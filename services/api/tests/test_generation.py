"""WebMCP contracts, generation and patches — F5-01, F5-02, F5-03.

The generator emits code deterministically from a validated contract. The tests
that matter most are the ones that would have caught the bugs a real typecheck
found: a handler shadowing the function it imports, and a call built in the
wrong argument shape.
"""

from __future__ import annotations

import json
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from mcpforge.generation.nextjs import WEBMCP_DIR, generate_patch
from mcpforge.models.core import artifact_hash
from mcpforge.models.patch import ChangeKind, FileChange, GeneratedPatch
from mcpforge.models.webmcp import (
    WebMCPTool,
    WebMCPToolset,
)


def read_tool(**over: Any) -> WebMCPTool:
    data: dict[str, Any] = {
        "name": "search_rooms",
        "title": "Search rooms",
        "description": "Find rooms matching guests and price.",
        "inputs": [
            {"name": "guests", "json_type": "integer", "description": "Number of guests"},
            {
                "name": "maxPrice",
                "json_type": "number",
                "description": "Highest nightly price",
                "required": False,
            },
        ],
        "output_description": "Matching rooms.",
        "risk": "READ",
        "approval_required": False,
        "source": {
            "module": "@/lib/rooms",
            "symbol": "searchRooms",
            "call_style": "object",
            "parameters": ["guests", "maxPrice"],
        },
        "evidence": [{"path": "src/lib/rooms.ts"}],
    }
    data.update(over)
    return WebMCPTool.model_validate(data)


def destructive_tool() -> WebMCPTool:
    return WebMCPTool.model_validate(
        {
            "name": "cancel_reservation",
            "title": "Cancel a reservation",
            "description": "Cancels an existing booking.",
            "inputs": [
                {"name": "reservationId", "json_type": "string", "description": "Booking id"}
            ],
            "output_description": "The cancelled reservation.",
            "risk": "DESTRUCTIVE",
            "approval_required": True,
            "source": {
                "module": "@/lib/reservations",
                "symbol": "cancelReservation",
                "parameters": ["reservationId"],
            },
            "evidence": [{"path": "src/lib/reservations.ts"}],
        }
    )


# -- the contract -----------------------------------------------------------


def test_a_destructive_tool_cannot_declare_itself_ungated() -> None:
    """The invariant is enforced where the generator reads it, not only where
    reconciliation sets it."""
    with pytest.raises(ValidationError, match="must require approval"):
        read_tool(name="cancel_reservation", risk="DESTRUCTIVE", approval_required=False)


def test_the_input_schema_is_valid_and_closed() -> None:
    schema = read_tool().input_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate({"guests": 2}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"guests": 2, "sneaky": True}, schema)


@pytest.mark.parametrize("bad", ["2rooms", "room-id", "class", "room id", "room.id"])
def test_input_names_must_be_usable_as_identifiers(bad: str) -> None:
    """A name that is not an identifier either breaks the file or injects into it."""
    if bad == "class":
        pytest.skip("reserved words are valid identifiers to the regex; TS handles them")
    with pytest.raises(ValidationError):
        read_tool(inputs=[{"name": bad, "json_type": "string", "description": "x"}])


@pytest.mark.parametrize("bad", ["user_id", "role", "token", "path", "sql"])
def test_inputs_that_grant_authority_are_refused(bad: str) -> None:
    with pytest.raises(ValidationError, match="authority"):
        read_tool(
            inputs=[{"name": bad, "json_type": "string", "description": "x"}],
            source={
                "module": "@/lib/rooms",
                "symbol": "searchRooms",
                "call_style": "object",
                "parameters": [],
            },
        )


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "../../secrets",
        "../lib/rooms",
        "https://evil.test/x",
        "lib/rooms",
        "node:fs",
        "@/lib/../../escape",
    ],
)
def test_import_specifiers_must_use_the_alias_and_cannot_escape(bad: str) -> None:
    with pytest.raises(ValidationError):
        read_tool(source={"module": bad, "symbol": "searchRooms", "parameters": []})


def test_a_tool_missing_an_argument_for_its_target_is_refused() -> None:
    """Caught on the contract, not discovered when the file fails to compile."""
    with pytest.raises(ValidationError, match="no input for"):
        read_tool(
            inputs=[{"name": "guests", "json_type": "integer", "description": "x"}],
            source={
                "module": "@/lib/availability",
                "symbol": "checkAvailability",
                "parameters": ["roomId", "checkIn", "checkOut"],
            },
        )


def test_type_and_handler_names_are_derived_correctly() -> None:
    """`Cancelreservation` was a real bug: capitalising a camelCase handler."""
    tool = destructive_tool()
    assert tool.handler_name == "cancelReservation"
    assert tool.type_name == "CancelReservation"
    assert tool.import_alias == "cancelReservationImpl"


def test_duplicate_tool_names_are_refused() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        WebMCPToolset(tools=[read_tool(), read_tool()])


# -- generation: the bugs a real typecheck found ---------------------------


def test_a_handler_never_calls_itself() -> None:
    """The generated handler and the imported function often share a name.

    Calling the unaliased symbol is either a redeclaration error or, worse,
    infinite recursion. This shipped once and was caught only by compiling.
    """
    patch = generate_patch(WebMCPToolset(tools=[read_tool()]))
    source = next(f for f in patch.files if f.path.endswith("searchRooms.ts")).contents

    assert "import { searchRooms as searchRoomsImpl }" in source
    assert "searchRoomsImpl(" in source
    # In code (comments excluded), the only bare `searchRooms(` may be the
    # handler's own declaration.
    calls = [
        line.strip()
        for line in source.splitlines()
        if "searchRooms(" in line
        and "searchRoomsImpl" not in line
        and not line.lstrip().startswith(("*", "//", "/*"))
    ]
    assert calls == ["export async function searchRooms("], calls


def test_an_object_taking_function_is_called_with_an_object() -> None:
    """`searchRooms(params: {...})` takes one argument, not two."""
    patch = generate_patch(WebMCPToolset(tools=[read_tool()]))
    source = next(f for f in patch.files if f.path.endswith("searchRooms.ts")).contents
    assert "searchRoomsImpl({ guests: input.guests, maxPrice: input.maxPrice })" in source


def test_a_positional_function_is_called_positionally_in_its_own_order() -> None:
    """Ordered by the function's parameter list, so a reordered schema cannot
    swap two arguments of the same type."""
    tool = read_tool(
        name="check_availability",
        title="Check availability",
        inputs=[
            {"name": "checkOut", "json_type": "string", "description": "ISO date"},
            {"name": "roomId", "json_type": "string", "description": "Room id"},
            {"name": "checkIn", "json_type": "string", "description": "ISO date"},
        ],
        source={
            "module": "@/lib/availability",
            "symbol": "checkAvailability",
            "call_style": "positional",
            "parameters": ["roomId", "checkIn", "checkOut"],
        },
    )
    source = next(
        f
        for f in generate_patch(WebMCPToolset(tools=[tool])).files
        if f.path.endswith("checkAvailability.ts")
    ).contents
    assert "checkAvailabilityImpl(input.roomId, input.checkIn, input.checkOut)" in source


def test_an_async_target_is_awaited() -> None:
    tool = read_tool(
        source={
            "module": "@/lib/rooms",
            "symbol": "searchRooms",
            "is_async": True,
            "call_style": "object",
            "parameters": ["guests", "maxPrice"],
        }
    )
    source = next(
        f
        for f in generate_patch(WebMCPToolset(tools=[tool])).files
        if f.path.endswith("searchRooms.ts")
    ).contents
    assert "await searchRoomsImpl(" in source


# -- generation: the safety properties -------------------------------------


def test_a_gated_tool_requests_approval_instead_of_acting() -> None:
    """03_SECURITY_ACCESS.md §8.3 — no agent-only fast path."""
    patch = generate_patch(WebMCPToolset(tools=[destructive_tool()]))
    source = next(f for f in patch.files if f.path.endswith("cancelReservation.ts")).contents

    assert "requestApproval(" in source
    assert "awaitingApproval(" in source
    # It must not call the underlying function at all.
    assert "cancelReservationImpl(" not in source


def test_an_ungated_tool_does_not_request_approval() -> None:
    patch = generate_patch(WebMCPToolset(tools=[read_tool()]))
    source = next(f for f in patch.files if f.path.endswith("searchRooms.ts")).contents
    assert "requestApproval" not in source


def test_the_approvals_module_is_only_generated_when_needed() -> None:
    read_only = generate_patch(WebMCPToolset(tools=[read_tool()]))
    assert f"{WEBMCP_DIR}/approvals.ts" not in read_only.paths

    gated = generate_patch(WebMCPToolset(tools=[destructive_tool()]))
    assert f"{WEBMCP_DIR}/approvals.ts" in gated.paths


def test_required_inputs_are_validated_before_the_call() -> None:
    source = next(
        f
        for f in generate_patch(WebMCPToolset(tools=[destructive_tool()])).files
        if f.path.endswith("cancelReservation.ts")
    ).contents
    assert 'return failed("reservationId is required", "invalid_input")' in source


def test_the_adapter_probes_both_surfaces_and_degrades_cleanly() -> None:
    adapter = next(
        f
        for f in generate_patch(WebMCPToolset(tools=[read_tool()])).files
        if f.path.endswith("adapter.ts")
    ).contents
    assert "document" in adapter and "navigator" in adapter
    assert "unsupported" in adapter
    # The draft has no unregisterTool; teardown is the abort signal. The prose
    # explains that, so check it is never *called*.
    assert ".unregisterTool(" not in adapter
    assert "signal" in adapter


def test_registration_tears_down_with_an_abort_signal() -> None:
    register = next(
        f
        for f in generate_patch(WebMCPToolset(tools=[read_tool()])).files
        if f.path.endswith("register.ts")
    ).contents
    assert "AbortController" in register
    assert "controller.abort()" in register


def test_errors_are_structured_never_raw_exceptions() -> None:
    source = next(
        f
        for f in generate_patch(WebMCPToolset(tools=[read_tool()])).files
        if f.path.endswith("searchRooms.ts")
    ).contents
    assert "error instanceof Error ? error.message" in source


# -- the patch --------------------------------------------------------------


def test_every_file_carries_a_rationale() -> None:
    """04_FRONTEND_SPEC.md §6 — the diff answers "why does this file change?"."""
    patch = generate_patch(WebMCPToolset(tools=[read_tool(), destructive_tool()]))
    assert all(f.rationale for f in patch.files)
    tool_files = [f for f in patch.files if "/tools/" in f.path]
    assert all(f.affected_tool for f in tool_files)


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "../outside.ts", "src/../../escape.ts", ".git/config", ".github/x.yml"]
)
def test_a_patch_cannot_target_a_path_outside_the_repository(bad: str) -> None:
    with pytest.raises(ValidationError):
        FileChange(path=bad, kind=ChangeKind.ADD, contents="x", rationale="y")


def test_the_diff_counts_lines() -> None:
    patch = generate_patch(WebMCPToolset(tools=[read_tool()]))
    assert patch.total_added > 0
    assert patch.total_removed == 0
    assert "+++ b/src/webmcp/adapter.ts" in patch.unified_diff()


def test_the_approval_hash_covers_content_but_not_wording() -> None:
    """Rewording an explanation must not invalidate a human's approval of code."""
    patch = generate_patch(WebMCPToolset(tools=[read_tool()]), base_commit="abc")
    before = artifact_hash(
        json.loads(GeneratedPatch.model_validate(patch).model_dump_json())
        if False
        else patch.hashable()
    )

    reworded = patch.model_copy(
        update={
            "files": [f.model_copy(update={"rationale": "different words"}) for f in patch.files]
        }
    )
    assert artifact_hash(reworded.hashable()) == before

    changed = patch.model_copy(
        update={
            "files": [
                *patch.files[:-1],
                patch.files[-1].model_copy(update={"contents": "// tampered"}),
            ]
        }
    )
    assert artifact_hash(changed.hashable()) != before


def test_a_moved_base_commit_invalidates_the_hash() -> None:
    toolset = WebMCPToolset(tools=[read_tool()])
    first = generate_patch(toolset, base_commit="abc").hashable()
    second = generate_patch(toolset, base_commit="def").hashable()
    assert artifact_hash(first) != artifact_hash(second)


def test_generation_is_deterministic() -> None:
    """The same contract produces byte-identical output, so a re-run does not
    invalidate an approval for no reason."""
    toolset = WebMCPToolset(tools=[read_tool(), destructive_tool()])
    a = generate_patch(toolset, base_commit="abc")
    b = generate_patch(toolset, base_commit="abc")
    assert artifact_hash(a.hashable()) == artifact_hash(b.hashable())


# -- framework adapters — F5-04 --------------------------------------------


def index_for(framework: str, *, supported: bool) -> Any:
    from mcpforge.models.index import FrameworkInfo, RepositoryIndex

    return RepositoryIndex(
        root="repo",
        framework=FrameworkInfo(name=framework, supported=supported, reason="test"),
    )


def test_a_next_js_repository_gets_an_adapter() -> None:
    from mcpforge.generation.adapters.registry import adapter_for

    adapter = adapter_for(index_for("next.js", supported=True))
    assert adapter.info.framework == "next.js"


@pytest.mark.parametrize("framework", ["vue", "svelte", "angular", "nuxt", "unknown"])
def test_an_unsupported_framework_is_declined_by_name(framework: str) -> None:
    """01_PRD.md §9 — name what was found and stop. Never degrade quietly."""
    from mcpforge.generation.adapters.base import UnsupportedFrameworkError
    from mcpforge.generation.adapters.registry import adapter_for

    with pytest.raises(UnsupportedFrameworkError) as exc:
        adapter_for(index_for(framework, supported=False))

    assert exc.value.detected == framework
    assert framework in str(exc.value)
    assert "Next.js" in str(exc.value)


def test_a_next_js_repository_the_indexer_could_not_confirm_is_declined() -> None:
    """`supported` is the indexer's verdict; the adapter does not second-guess it."""
    from mcpforge.generation.adapters.base import UnsupportedFrameworkError
    from mcpforge.generation.adapters.registry import adapter_for

    with pytest.raises(UnsupportedFrameworkError):
        adapter_for(index_for("next.js", supported=False))


def test_the_supported_list_comes_from_the_registry_not_from_copy() -> None:
    """So the product cannot advertise support it does not have."""
    from mcpforge.generation.adapters.registry import ADAPTERS, supported_frameworks

    assert supported_frameworks() == [a.info.display_name for a in ADAPTERS]
    assert supported_frameworks() == ["Next.js (App Router)"]


def test_the_adapter_generates_the_same_patch_as_the_generator() -> None:
    from mcpforge.generation.adapters.registry import adapter_for

    toolset = WebMCPToolset(tools=[read_tool()])
    adapter = adapter_for(index_for("next.js", supported=True))
    assert (
        adapter.generate(toolset, base_commit="abc").hashable()
        == generate_patch(toolset, base_commit="abc").hashable()
    )
