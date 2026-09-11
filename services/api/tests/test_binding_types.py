"""A binding must fit the real declaration's types — F9-01, first live run.

The first paid analysis run on the demo project generated code the demo's own
`tsc` rejected (typecheck exit 2, build exit 1). The binding in
`orchestration/toolset.py` matched parameters by name only and copied the
model's JSON type straight through, and for a single-object call it did not
check names at all.

The reproduction corrected the first diagnosis. `book_room` → `createReservation`
is WRITE, so its generated handler only calls `requestApproval` and its types
never reach `tsc`. The tool that calls straight through with an object is
`search_rooms` → `searchRooms(params: { guests?: number; maxPrice?: number })`,
and two plans the old binding accepted fail real `tsc`:

- `guests` declared as a string →
  `error TS2322: Type 'string | undefined' is not assignable to type 'number | undefined'.`
- an extra field `checkIn` →
  `error TS2353: Object literal may only specify known properties, and 'checkIn'
  does not exist in type '{ guests?: number | undefined; maxPrice?: number | undefined; }'.`

Both are now refused at binding, which the architect runs as its own
verification — so an ill-typed plan costs the model a retry, and is never
offered to a human. The real-`tsc` tests at the bottom are what make that more
than a claim: the same compiler that failed the live run passes the correctly
typed plan and fails the ones the binding now refuses.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from mcpforge.agents.architect import (
    ArchitectInput,
    WorkflowArchitect,
    reconcile_risk,
    render_parameters,
)
from mcpforge.agents.base import AgentOutputError
from mcpforge.agents.validator import harness_files
from mcpforge.gemini.fake import FakeGeminiProvider
from mcpforge.gemini.provider import TraceContext
from mcpforge.generation.nextjs import generate_patch
from mcpforge.indexing.indexer import build_index
from mcpforge.indexing.parser import parse_source
from mcpforge.models.analysis import CodebaseAnalysis
from mcpforge.models.index import (
    FileKind,
    FileNode,
    FrameworkInfo,
    RepositoryIndex,
    Symbol,
    SymbolKind,
    TsType,
)
from mcpforge.models.toolplan import ToolPlan
from mcpforge.models.webmcp import WebMCPTool, WebMCPToolset
from mcpforge.orchestration.toolset import ToolsetConversionError, toolset_from_plan

REPO_ROOT = Path(__file__).resolve().parents[3]
DEMO = REPO_ROOT / "fixtures" / "demo-hotel-app"
NODE_MODULES = REPO_ROOT / "node_modules"
TSC = NODE_MODULES / "typescript" / "bin" / "tsc"
TRACE = TraceContext(project_id="p", run_id="r", agent="architect", step="s")


@pytest.fixture(scope="module")
def index() -> RepositoryIndex:
    return build_index(DEMO)


def _param(name: str, json_type: str, *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "json_type": json_type, "description": name, "required": required}


def search_rooms(*params: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "search_rooms",
        "title": "Search rooms",
        "description": "Find rooms for a party within a budget.",
        "workflow_id": "search_rooms",
        "maps_to_function": "searchRooms",
        "parameters": list(params),
        "output_description": "Matching rooms.",
        "risk": "READ",
        "evidence": [{"path": "src/lib/rooms.ts"}],
    }


GOOD_SEARCH = search_rooms(
    _param("guests", "integer", required=False), _param("maxPrice", "number", required=False)
)
#: TS2322 in the reproduction.
GUESTS_AS_STRING = search_rooms(
    _param("guests", "string", required=False), _param("maxPrice", "number", required=False)
)
#: TS2353 in the reproduction.
UNKNOWN_FIELD = search_rooms(
    _param("guests", "integer", required=False), _param("checkIn", "string", required=False)
)

BOOKING_FIELDS = {
    "roomId": "string",
    "guestName": "string",
    "guestEmail": "string",
    "checkIn": "string",
    "checkOut": "string",
    "guests": "integer",
}


def book_room(fields: dict[str, str], *, optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    return {
        "name": "book_room",
        "title": "Book a room",
        "description": "Reserve a room for a guest.",
        "workflow_id": "book",
        "maps_to_function": "createReservation",
        "parameters": [_param(n, t, required=n not in optional) for n, t in fields.items()],
        "output_description": "The reservation.",
        "risk": "WRITE",
        "evidence": [{"path": "src/lib/reservations.ts"}],
    }


def bind(index: RepositoryIndex, *tools: dict[str, Any]) -> WebMCPToolset:
    plan = ToolPlan.model_validate({"tools": list(tools)})
    return toolset_from_plan(reconcile_risk(plan)[0], index)


# ---------------------------------------------------------------------------
# The index records types
# ---------------------------------------------------------------------------


def test_the_index_records_the_demo_signatures_types(index: RepositoryIndex) -> None:
    found = index.find_symbol("searchRooms")
    assert found is not None
    (params,) = found[1].signature
    assert params.name == "params" and params.ts_type is TsType.OBJECT
    assert params.fields is not None
    assert [(f.name, f.ts_type, f.optional) for f in params.fields] == [
        ("guests", TsType.NUMBER, True),
        ("maxPrice", TsType.NUMBER, True),
    ]

    found = index.find_symbol("createReservation")
    assert found is not None
    (booking,) = found[1].signature
    assert booking.fields is not None
    assert {f.name: (f.ts_type, f.optional) for f in booking.fields} == {
        "roomId": (TsType.STRING, False),
        "guestName": (TsType.STRING, False),
        "guestEmail": (TsType.STRING, False),
        "checkIn": (TsType.STRING, False),
        "checkOut": (TsType.STRING, False),
        "guests": (TsType.NUMBER, False),
    }


def test_what_syntax_cannot_settle_is_recorded_as_unknown_never_guessed() -> None:
    parsed = parse_source(
        "export function f(a: string, b?: number, c = 3, d: string[], "
        "e: 'x' | 'y', g: Array<string>, h: Opts, i: (boolean), "
        "j: { k: string; [key: string]: unknown }, l: any, ...rest: number[]) {}\n",
        tsx=False,
    )
    (symbol,) = parsed.symbols
    types = {p.name: (p.ts_type, p.optional) for p in symbol.signature}
    assert types == {
        "a": (TsType.STRING, False),
        "b": (TsType.NUMBER, True),
        "c": (TsType.UNKNOWN, True),  # a default, no annotation
        "d": (TsType.ARRAY, False),
        "e": (TsType.UNKNOWN, False),  # a union
        "g": (TsType.UNKNOWN, False),  # a generic
        "h": (TsType.UNKNOWN, False),  # a named alias or interface
        "i": (TsType.BOOLEAN, False),
        "j": (TsType.OBJECT, False),
        "l": (TsType.ANY, False),
        "...rest": (TsType.UNKNOWN, True),
    }
    by_name = {p.name: p for p in symbol.signature}
    # An index signature admits keys the syntax does not list: shape unknown.
    assert by_name["j"].fields is None
    assert [p.name for p in symbol.signature] == symbol.params


# ---------------------------------------------------------------------------
# The binding refuses what does not fit
# ---------------------------------------------------------------------------


def test_search_rooms_with_guests_as_a_string_is_refused_at_binding(
    index: RepositoryIndex,
) -> None:
    """The live run's TS2322, refused before generation can be reached."""
    with pytest.raises(ToolsetConversionError, match=r"'guests' as string.*declared number"):
        bind(index, GUESTS_AS_STRING)


def test_search_rooms_with_a_field_the_object_lacks_is_refused_at_binding(
    index: RepositoryIndex,
) -> None:
    """The live run's TS2353. The name check never looked inside an object."""
    with pytest.raises(ToolsetConversionError, match=r"'checkIn', which searchRooms"):
        bind(index, UNKNOWN_FIELD)


def test_a_correctly_typed_search_rooms_binds_with_nothing_left_unchecked(
    index: RepositoryIndex,
) -> None:
    (tool,) = bind(index, GOOD_SEARCH).tools
    assert tool.source.parameters == ["guests", "maxPrice"]
    assert tool.source.unchecked == []


def test_an_optional_field_may_be_omitted(index: RepositoryIndex) -> None:
    (tool,) = bind(index, search_rooms(_param("maxPrice", "number", required=False))).tools
    assert tool.source.parameters == ["maxPrice"]


def test_an_ill_typed_book_room_is_refused_even_though_it_would_compile(
    index: RepositoryIndex,
) -> None:
    """Pins **schema correctness, not compilation.**

    `book_room` is WRITE, so its generated handler only requests approval and
    never calls `createReservation` — the reproduction showed both plans below
    pass real `tsc`. They are refused anyway: the schema a human approves is
    the contract an agent is given, and one saying `guests` is a string, or
    that a booking needs no email, misdescribes the function it stands for.
    """
    with pytest.raises(ToolsetConversionError, match=r"'guests' as string.*declared number"):
        bind(index, book_room({**BOOKING_FIELDS, "guests": "string"}))

    without_email = {k: v for k, v in BOOKING_FIELDS.items() if k != "guestEmail"}
    with pytest.raises(ToolsetConversionError, match=r"omits required field\(s\) \['guestEmail'\]"):
        bind(index, book_room(without_email))


def test_a_required_field_may_not_be_declared_optional(index: RepositoryIndex) -> None:
    with pytest.raises(ToolsetConversionError, match=r"marks 'guestEmail' optional"):
        bind(index, book_room(BOOKING_FIELDS, optional=frozenset({"guestEmail"})))


def test_a_correctly_typed_book_room_binds(index: RepositoryIndex) -> None:
    (tool,) = bind(index, book_room(BOOKING_FIELDS)).tools
    assert sorted(tool.source.parameters) == sorted(BOOKING_FIELDS)
    assert tool.source.unchecked == []


def _availability(*params: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "check_availability",
        "title": "Check availability",
        "description": "Is a room free.",
        "workflow_id": "availability",
        "maps_to_function": "checkAvailability",
        "parameters": list(params),
        "output_description": "Availability.",
        "risk": "READ",
        "evidence": [{"path": "src/lib/availability.ts"}],
    }


def test_every_positional_parameter_is_type_checked_not_only_the_first(
    index: RepositoryIndex,
) -> None:
    """`checkAvailability(roomId, checkIn, checkOut)` with the *second* one
    wrong. A check that only compared the first parameter would pass this."""
    with pytest.raises(ToolsetConversionError, match=r"'checkIn' as number.*declared string"):
        bind(
            index,
            _availability(
                _param("roomId", "string"),
                _param("checkIn", "number"),
                _param("checkOut", "string"),
            ),
        )
    with pytest.raises(ToolsetConversionError, match=r"'checkOut' as boolean.*declared string"):
        bind(
            index,
            _availability(
                _param("roomId", "string"),
                _param("checkIn", "string"),
                _param("checkOut", "boolean"),
            ),
        )
    (tool,) = bind(
        index,
        _availability(
            _param("roomId", "string"), _param("checkIn", "string"), _param("checkOut", "string")
        ),
    ).tools
    assert tool.source.parameters == ["roomId", "checkIn", "checkOut"]


def test_a_positional_type_mismatch_is_refused(index: RepositoryIndex) -> None:
    check = {
        "name": "check_availability",
        "title": "Check availability",
        "description": "Is a room free.",
        "workflow_id": "availability",
        "maps_to_function": "checkAvailability",
        "parameters": [
            _param("roomId", "number"),
            _param("checkIn", "string"),
            _param("checkOut", "string"),
        ],
        "output_description": "Availability.",
        "risk": "READ",
        "evidence": [{"path": "src/lib/availability.ts"}],
    }
    with pytest.raises(ToolsetConversionError, match=r"'roomId' as number.*declared string"):
        bind(index, check)


# ---------------------------------------------------------------------------
# Unknown is not refused on type, and is recorded as not checked
# ---------------------------------------------------------------------------

UNREADABLE_SOURCE = """\
import type { OrderRef } from "@/lib/types";

export function findOrders(params: { status: "open" | "closed"; limit?: number; tags: string[] }) {
  return [params];
}

export function orderFor(ref: OrderRef) {
  return ref;
}
"""


def _unreadable_index(symbols: list[Symbol] | None = None) -> RepositoryIndex:
    parsed = parse_source(UNREADABLE_SOURCE, tsx=False)
    return RepositoryIndex(
        root=".",
        framework=FrameworkInfo(name="next.js", supported=True),
        files=[
            FileNode(
                path="src/lib/orders.ts",
                kind=FileKind.SERVICE,
                language="ts",
                lines=UNREADABLE_SOURCE.count("\n"),
                symbols=symbols if symbols is not None else parsed.symbols,
                exports=parsed.exports,
            )
        ],
    )


def _orders_tool(function: str, *params: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "find_orders",
        "title": "Find orders",
        "description": "Find orders.",
        "workflow_id": "orders",
        "maps_to_function": function,
        "parameters": list(params),
        "output_description": "Orders.",
        "risk": "READ",
        "evidence": [{"path": "src/lib/orders.ts"}],
    }


def test_an_unknown_field_type_is_not_refused_and_is_recorded_as_not_checked() -> None:
    # `status` is a union, so even `boolean` is not refused on type: the check
    # cannot say it is wrong. It says it did not look.
    (tool,) = bind(
        _unreadable_index(),
        _orders_tool(
            "findOrders",
            _param("status", "boolean"),
            _param("tags", "array"),
        ),
    ).tools
    notes = " | ".join(tool.source.unchecked)
    assert "findOrders(params: {...}).status" in notes and "was not type-checked" in notes
    # The array's kind was compared; its element type was not, and it says so.
    assert "tags is an array" in notes and "element type was not checked" in notes


def test_an_unknown_type_still_needs_its_required_field_present() -> None:
    """Unknown relaxes the *type* comparison only, never the name rules."""
    with pytest.raises(ToolsetConversionError, match=r"omits required field\(s\) \['status'\]"):
        bind(_unreadable_index(), _orders_tool("findOrders", _param("tags", "array")))


def test_a_named_parameter_type_is_recorded_as_not_checked() -> None:
    (tool,) = bind(_unreadable_index(), _orders_tool("orderFor", _param("ref", "string"))).tools
    assert any("orderFor(ref)" in note for note in tool.source.unchecked)


def test_an_index_without_types_checks_nothing_and_says_so() -> None:
    """An index persisted before types were recorded is not read as all-clear."""
    legacy = [
        s.model_copy(update={"signature": []}) if s.kind is SymbolKind.FUNCTION else s
        for s in parse_source(UNREADABLE_SOURCE, tsx=False).symbols
    ]
    (tool,) = bind(
        _unreadable_index(legacy), _orders_tool("orderFor", _param("ref", "string"))
    ).tools
    assert any("recorded no parameter types" in note for note in tool.source.unchecked)


# ---------------------------------------------------------------------------
# The prompt shows the types; the retry is bounded
# ---------------------------------------------------------------------------


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
                }
            ],
        }
    )


def test_the_prompt_shows_the_real_parameter_and_field_types(
    index: RepositoryIndex, analysis: CodebaseAnalysis
) -> None:
    prompt = WorkflowArchitect(FakeGeminiProvider()).build_prompt(
        ArchitectInput(index=index, analysis=analysis, selected_workflow_ids=["search_rooms"])
    )
    assert "searchRooms(params: { guests?: number; maxPrice?: number })" in prompt
    assert (
        "createReservation(input: { roomId: string; guestName: string; guestEmail: string; "
        "checkIn: string; checkOut: string; guests: number })"
    ) in prompt
    assert "checkAvailability(roomId: string, checkIn: string, checkOut: string)" in prompt


def test_the_prompt_shows_an_unreadable_type_as_unknown() -> None:
    found = _unreadable_index().find_symbol("findOrders")
    assert found is not None
    assert render_parameters(found[1]) == [
        "params: { status: unknown-type; limit?: number; tags: array }"
    ]


async def test_an_ill_typed_plan_costs_a_retry_and_the_next_one_is_kept(
    index: RepositoryIndex, analysis: CodebaseAnalysis
) -> None:
    provider = FakeGeminiProvider(
        [{"tools": [GUESTS_AS_STRING], "notes": []}, {"tools": [GOOD_SEARCH], "notes": []}]
    )
    plan, record = await WorkflowArchitect(provider).run(
        ArchitectInput(index=index, analysis=analysis, selected_workflow_ids=["search_rooms"]),
        TRACE,
    )
    assert [p.json_type for p in plan.tools[0].parameters] == ["integer", "number"]
    assert record.evidence_rejections == 1
    assert "'guests' as string" in record.notes[0]


async def test_a_plan_that_never_types_correctly_exhausts_the_bounded_retry(
    index: RepositoryIndex, analysis: CodebaseAnalysis
) -> None:
    agent = WorkflowArchitect(FakeGeminiProvider([{"tools": [UNKNOWN_FIELD], "notes": []}] * 3))
    with pytest.raises(AgentOutputError):
        await agent.run(
            ArchitectInput(index=index, analysis=analysis, selected_workflow_ids=["search_rooms"]),
            TRACE,
        )
    assert agent.max_output_retries == 2


# ---------------------------------------------------------------------------
# The real compiler — the property the live run lacked
# ---------------------------------------------------------------------------

INTEGRATION_ENV = "MCPFORGE_VALIDATION_INTEGRATION_REQUIRED"


def _require_compiler() -> str:
    node = shutil.which("node") or ("/usr/bin/node" if Path("/usr/bin/node").exists() else None)
    missing = None
    if not TSC.is_file():
        missing = "node_modules/typescript is not installed; run npm ci at the repository root"
    elif node is None:
        missing = "node is not installed"
    if missing is not None:
        if os.environ.get(INTEGRATION_ENV) == "1":
            pytest.fail(f"{INTEGRATION_ENV}=1 and {missing}")
        pytest.skip(missing)
    assert node is not None
    return node


def _tsc(node: str, app: Path, toolset: WebMCPToolset) -> subprocess.CompletedProcess[str]:
    """The demo app, the generated patch and the validator harness — the files
    validation writes — typechecked by the demo's own `tsc` over the dependency
    tree the pipeline hard-links into the demo leg."""
    shutil.copytree(
        DEMO, app, ignore=shutil.ignore_patterns("node_modules", ".next", "tsconfig.tsbuildinfo")
    )
    subprocess.run(  # noqa: S603
        ["cp", "-al", str(NODE_MODULES), str(app / "node_modules")],  # noqa: S607
        check=True,
    )
    written = {c.path: c.contents for c in generate_patch(toolset, base_commit="demo").files}
    written.update(harness_files(toolset))
    for relative, contents in written.items():
        (app / relative).parent.mkdir(parents=True, exist_ok=True)
        (app / relative).write_text(contents, encoding="utf-8")
    return subprocess.run(  # noqa: S603
        [node, str(app / "node_modules" / "typescript" / "bin" / "tsc"), "--noEmit"],
        cwd=app,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_a_correctly_typed_search_rooms_plan_passes_real_tsc(
    index: RepositoryIndex, tmp_path: Path
) -> None:
    node = _require_compiler()
    result = _tsc(node, tmp_path / "app", bind(index, GOOD_SEARCH))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("plan", "typescript_error"),
    [(GUESTS_AS_STRING, "TS2322"), (UNKNOWN_FIELD, "TS2353")],
    ids=["guests-as-string", "unknown-field"],
)
def test_a_plan_real_tsc_rejects_never_gets_past_binding(
    index: RepositoryIndex, tmp_path: Path, plan: dict[str, Any], typescript_error: str
) -> None:
    """Both halves, so neither can drift alone.

    The binding refuses the plan. And the tool the old binding produced from it
    — the same contract, built directly, as `toolset_from_plan` used to — is
    compiled by the real `tsc`, which must reject it with the live run's error.
    If `tsc` ever accepted it, the refusal would be guarding nothing.
    """
    with pytest.raises(ToolsetConversionError):
        bind(index, plan)

    node = _require_compiler()
    good = bind(index, GOOD_SEARCH).tools[0]
    as_the_old_binding_made_it = WebMCPTool.model_validate(
        {
            **good.model_dump(),
            "inputs": [
                {k: p[k] for k in ("name", "json_type", "description", "required")}
                for p in plan["parameters"]
            ],
            "source": {
                **good.source.model_dump(),
                "parameters": [p["name"] for p in plan["parameters"]],
            },
        }
    )
    result = _tsc(node, tmp_path / "app", WebMCPToolset(tools=[as_the_old_binding_made_it]))
    output = result.stdout + result.stderr
    assert result.returncode != 0, "real tsc accepted a plan the binding refuses"
    assert typescript_error in output, output
