"""Repository indexer — F3-05, and the demo project — F3-07.

Run against the real fixture app, not a synthetic string, so the index has to
cope with a genuine Next.js layout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcpforge.indexing.indexer import (
    build_index,
    classify_file,
    detect_framework,
    route_path_for,
)
from mcpforge.models.index import FileKind, RepositoryIndex, SymbolKind

DEMO = Path(__file__).resolve().parents[3] / "fixtures" / "demo-hotel-app"


@pytest.fixture(scope="module")
def index() -> RepositoryIndex:
    return build_index(DEMO)


# -- the demo project itself ------------------------------------------------


def test_the_demo_app_exists_and_is_real(index: RepositoryIndex) -> None:
    """F3-07: a real app with genuine business logic, not stubs."""
    assert DEMO.is_dir()
    assert index.framework.name == "next.js"
    assert index.framework.supported is True


# -- framework detection ----------------------------------------------------


def test_nextjs_is_detected_and_supported() -> None:
    info = detect_framework(json.dumps({"dependencies": {"next": "^16.3.4", "react": "^19"}}))
    assert info.name == "next.js"
    assert info.version == "16.3.4"
    assert info.supported is True


@pytest.mark.parametrize(
    ("dependency", "expected"),
    [("vue", "vue"), ("svelte", "svelte"), ("@angular/core", "angular"), ("nuxt", "nuxt")],
)
def test_other_frameworks_are_detected_and_declined(dependency: str, expected: str) -> None:
    """01_PRD.md §9 — name the framework and decline, never degrade quietly."""
    info = detect_framework(json.dumps({"dependencies": {dependency: "^3"}}))
    assert info.name == expected
    assert info.supported is False
    assert "not supported yet" in info.reason


def test_a_missing_package_json_is_reported_honestly() -> None:
    info = detect_framework(None)
    assert info.supported is False
    assert "could not be determined" in info.reason


def test_invalid_package_json_does_not_crash_the_index() -> None:
    assert detect_framework("{not json").supported is False


# -- routes -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/app/page.tsx", "/"),
        ("app/page.tsx", "/"),
        ("src/app/book/page.tsx", "/book"),
        ("src/app/api/rooms/route.ts", "/api/rooms"),
        ("src/app/(marketing)/about/page.tsx", "/about"),
        ("src/lib/rooms.ts", None),
    ],
)
def test_route_paths_are_derived_from_the_file_tree(path: str, expected: str | None) -> None:
    assert route_path_for(path) == expected


def test_the_demo_app_routes_are_found(index: RepositoryIndex) -> None:
    paths = {f.route_path for f in index.routes if f.route_path}
    assert paths == {"/", "/book"}


def test_api_handlers_are_found_with_their_http_methods(index: RepositoryIndex) -> None:
    handlers = {f.route_path: sorted(f.http_methods) for f in index.api_handlers}
    assert handlers == {
        "/api/rooms": ["GET"],
        "/api/reservations": ["GET", "POST"],
        "/api/reservations/cancel": ["POST"],
    }


# -- classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("src/app/page.tsx", FileKind.ROUTE),
        ("src/app/api/rooms/route.ts", FileKind.API_HANDLER),
        ("src/components/RoomCard.tsx", FileKind.COMPONENT),
        ("src/lib/rooms.ts", FileKind.SERVICE),
        ("src/lib/types.ts", FileKind.MODEL),
        ("package.json", FileKind.CONFIG),
        ("src/lib/rooms.test.ts", FileKind.TEST),
        ("src/app/globals.css", FileKind.STYLE),
    ],
)
def test_files_are_classified_by_role(path: str, kind: FileKind) -> None:
    assert classify_file(path, []) is kind


def test_a_types_only_file_under_lib_is_a_model_not_a_service(index: RepositoryIndex) -> None:
    """src/lib/types.ts holds only interfaces, so it is not business logic."""
    types_file = next(f for f in index.files if f.path == "src/lib/types.ts")
    assert types_file.kind is FileKind.MODEL


# -- symbols ----------------------------------------------------------------


def test_business_functions_are_found_with_their_parameters(index: RepositoryIndex) -> None:
    """This is what the Workflow Architect maps a tool onto."""
    found = index.find_symbol("createReservation")
    assert found is not None
    file, symbol = found
    assert file.path == "src/lib/reservations.ts"
    assert symbol.kind is SymbolKind.FUNCTION
    assert symbol.exported is True
    assert symbol.params == ["input"]


@pytest.mark.parametrize(
    "name",
    ["searchRooms", "checkAvailability", "createReservation", "cancelReservation", "listRooms"],
)
def test_every_demo_workflow_function_is_indexed(index: RepositoryIndex, name: str) -> None:
    assert index.find_symbol(name) is not None


def test_a_function_that_does_not_exist_is_not_found(index: RepositoryIndex) -> None:
    """How a hallucinated reference gets rejected deterministically."""
    assert index.find_symbol("deleteAllCustomerData") is None


def test_react_components_are_distinguished_from_plain_functions(
    index: RepositoryIndex,
) -> None:
    found = index.find_symbol("RoomCard")
    assert found is not None
    assert found[1].kind is SymbolKind.COMPONENT

    found_fn = index.find_symbol("nightsBetween")
    assert found_fn is not None
    assert found_fn[1].kind is SymbolKind.FUNCTION


def test_interfaces_are_indexed(index: RepositoryIndex) -> None:
    found = index.find_symbol("Reservation")
    assert found is not None
    assert found[1].kind is SymbolKind.INTERFACE


# -- relationships ----------------------------------------------------------


def test_the_frontend_to_backend_call_is_mapped(index: RepositoryIndex) -> None:
    """The booking form posts to the reservations handler. Joining those is how
    a workflow gets recognised as one workflow rather than two files."""
    form = next(f for f in index.files if f.path.endswith("BookingForm.tsx"))
    assert [(c.method, c.url) for c in form.call_sites] == [("POST", "/api/reservations")]


def test_the_dependency_graph_resolves_repository_imports(index: RepositoryIndex) -> None:
    assert "src/lib/reservations.ts" in index.dependency_graph["src/app/api/reservations/route.ts"]
    assert "src/lib/types.ts" in index.dependency_graph["src/lib/rooms.ts"]


def test_external_packages_are_not_in_the_graph(index: RepositoryIndex) -> None:
    """next and react are imports, but they are not files in this repository."""
    for targets in index.dependency_graph.values():
        for target in targets:
            assert not target.startswith("next")
            assert not target.startswith("react")


# -- the binding rule -------------------------------------------------------


def test_the_index_contains_no_file_contents(index: RepositoryIndex) -> None:
    """02_ARCHITECTURE.md §5 — structure, not source.

    If bodies leaked into the index they would reach a prompt without passing
    through retrieval, which is the step that enforces the budget.
    """
    serialized = index.model_dump_json()
    for body_fragment in (
        "RESERVATIONS.set",
        "pricePerNight: 120",
        "Number.isNaN",
        "export default function HomePage",
    ):
        assert body_fragment not in serialized, f"file body leaked into the index: {body_fragment}"


def test_build_output_and_dependencies_are_absent(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / "react").mkdir(parents=True)
    (tmp_path / "node_modules" / "react" / "index.js").write_text("module.exports={};")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const a = 1;")
    (tmp_path / "package.json").write_text('{"dependencies":{"next":"16"}}')

    index = build_index(tmp_path)
    assert all("node_modules" not in f.path for f in index.files)


def test_secrets_never_reach_the_index(tmp_path: Path) -> None:
    """Filtering runs before parsing, so a credential file is never indexed."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const a = 1;")
    (tmp_path / "package.json").write_text('{"dependencies":{"next":"16"}}')
    (tmp_path / ".env").write_text("GEMINI_API_KEY=" + "AQ." + "x" * 40)

    index = build_index(tmp_path)
    assert ".env" in index.quarantined_paths
    assert all(f.path != ".env" for f in index.files)
    assert "AQ." not in index.model_dump_json()
