"""TypeScript/JavaScript parsing with tree-sitter — 02_ARCHITECTURE.md §5.

Syntax, not type resolution. That is deliberate: tree-sitter needs no Node
process, is fast, and tolerates syntax errors, which matters when indexing
someone else's repository. Full type semantics would need a `ts-morph` sidecar,
which is documented as possible future work and is not assumed to exist.

Extracts signatures and relationships. Never stores a function body.
"""

from __future__ import annotations

import re
from functools import lru_cache

import tree_sitter_typescript as ts_typescript
from tree_sitter import Language, Node, Parser

from mcpforge.models.index import CallSite, Symbol, SymbolKind


@lru_cache(maxsize=2)
def _parser(tsx: bool) -> Parser:
    language = Language(
        ts_typescript.language_tsx() if tsx else ts_typescript.language_typescript()
    )
    return Parser(language)


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _is_exported(node: Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == "export_statement":
            return True
        if parent.type in ("program", "statement_block"):
            return False
        parent = parent.parent
    return False


def _params_of(node: Node, source: bytes) -> list[str]:
    """Parameter names only — enough to design a schema, not enough to leak logic."""
    params_node = node.child_by_field_name("parameters")
    if params_node is None:
        return []
    names: list[str] = []
    for child in params_node.named_children:
        identifier = child
        if child.type in ("required_parameter", "optional_parameter"):
            pattern = child.child_by_field_name("pattern")
            if pattern is not None:
                identifier = pattern
        names.append(_text(identifier, source).split(":")[0].strip())
    return [n for n in names if n]


def _looks_like_component(name: str, body: str) -> bool:
    """A React component is an exported function starting with a capital that
    returns JSX. Naming alone is not enough, so the body is checked for a tag."""
    return bool(name[:1].isupper() and re.search(r"<[A-Za-z][\w.]*[\s/>]", body))


class ParsedFile:
    def __init__(self) -> None:
        self.symbols: list[Symbol] = []
        self.imports: list[str] = []
        self.exports: list[str] = []
        self.jsx_elements: list[str] = []
        self.call_sites: list[CallSite] = []


def parse_source(source_text: str, *, tsx: bool = True) -> ParsedFile:
    source = source_text.encode("utf-8")
    tree = _parser(tsx).parse(source)
    result = ParsedFile()

    def visit(node: Node) -> None:
        if node.type == "import_statement":
            source_node = node.child_by_field_name("source")
            if source_node is not None:
                result.imports.append(_text(source_node, source).strip("\"'"))

        elif node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node, source)
                body = _text(node, source)
                exported = _is_exported(node)
                kind = (
                    SymbolKind.COMPONENT
                    if _looks_like_component(name, body)
                    else SymbolKind.FUNCTION
                )
                result.symbols.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        exported=exported,
                        is_async="async" in body[: body.index("function") + 8],
                        params=_params_of(node, source),
                    )
                )
                if exported:
                    result.exports.append(name)

        elif node.type == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node, source)
                result.symbols.append(
                    Symbol(
                        name=name,
                        kind=SymbolKind.CLASS,
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        exported=_is_exported(node),
                    )
                )
                if _is_exported(node):
                    result.exports.append(name)

        elif node.type in ("interface_declaration", "type_alias_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _text(name_node, source)
                result.symbols.append(
                    Symbol(
                        name=name,
                        kind=SymbolKind.INTERFACE
                        if node.type == "interface_declaration"
                        else SymbolKind.TYPE,
                        line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        exported=_is_exported(node),
                    )
                )
                if _is_exported(node):
                    result.exports.append(name)

        elif node.type == "lexical_declaration":
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                value_node = declarator.child_by_field_name("value")
                if name_node is None:
                    continue
                name = _text(name_node, source)
                body = _text(declarator, source)
                is_fn = value_node is not None and value_node.type in (
                    "arrow_function",
                    "function_expression",
                )
                if is_fn:
                    kind = (
                        SymbolKind.COMPONENT
                        if _looks_like_component(name, body)
                        else SymbolKind.FUNCTION
                    )
                    params = _params_of(value_node, source) if value_node else []
                else:
                    kind = SymbolKind.CONST
                    params = []
                exported = _is_exported(node)
                result.symbols.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        line=declarator.start_point[0] + 1,
                        end_line=declarator.end_point[0] + 1,
                        exported=exported,
                        is_async=body.lstrip().startswith("async") or " async " in body[:80],
                        params=params,
                    )
                )
                if exported:
                    result.exports.append(name)

        elif node.type in ("jsx_opening_element", "jsx_self_closing_element"):
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                result.jsx_elements.append(_text(name_node, source))

        elif node.type == "call_expression":
            function_node = node.child_by_field_name("function")
            if function_node is not None and _text(function_node, source) in ("fetch", "axios"):
                call_text = _text(node, source)
                url_match = re.search(r"""["'`]([^"'`]+)["'`]""", call_text)
                method_match = re.search(r"""method:\s*["'](\w+)["']""", call_text)
                if url_match:
                    result.call_sites.append(
                        CallSite(
                            line=node.start_point[0] + 1,
                            method=(method_match.group(1).upper() if method_match else "GET"),
                            url=url_match.group(1),
                        )
                    )

        for child in node.children:
            visit(child)

    visit(tree.root_node)
    result.jsx_elements = sorted(set(result.jsx_elements))
    result.imports = sorted(set(result.imports))
    return result
