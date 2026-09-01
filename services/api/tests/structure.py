"""Helpers for tests that assert structural rules about the source.

Several rules in 02_ARCHITECTURE.md and 03_SECURITY_ACCESS.md are about what the
code *does*, not what it says — no vendor SDK here, no key path there. Grepping
text keeps tripping over the documentation that explains those very rules, so
these helpers read the AST instead.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def python_files(*, under: str = "mcpforge", exclude: tuple[str, ...] = ()) -> list[pathlib.Path]:
    return [p for p in (SRC / under).rglob("*.py") if p.name not in exclude]


def imported_modules(path: pathlib.Path) -> list[tuple[int, str]]:
    """Every module named by an import statement, with its line number."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


def files_importing(
    prefixes: tuple[str, ...], *, under: str = "mcpforge", exclude: tuple[str, ...] = ()
) -> list[str]:
    """Files that actually import one of these module prefixes."""
    offenders: list[str] = []
    for path in python_files(under=under, exclude=exclude):
        for lineno, module in imported_modules(path):
            if any(module == p or module.startswith(f"{p}.") for p in prefixes):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: imports {module}")
    return offenders


def code_lines(path: pathlib.Path) -> list[tuple[int, str]]:
    """Executable source lines only — no comments, no docstrings."""
    tree = ast.parse(path.read_text())
    docstring_ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            first = node.body[0]
            docstring_ranges.append((first.lineno, first.end_lineno or first.lineno))

    out: list[tuple[int, str]] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        if any(start <= lineno <= end for start, end in docstring_ranges):
            continue
        code = raw.split("#", 1)[0].strip()
        if code:
            out.append((lineno, code))
    return out
