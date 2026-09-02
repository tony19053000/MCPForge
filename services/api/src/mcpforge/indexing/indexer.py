"""Repository indexing — 02_ARCHITECTURE.md §5.

    Repository → deterministic index → relevant context → Gemini

Never `repository → dump into LLM`. Filtering runs first, so a quarantined file
is never read; this module only ever sees what `filter_tree` allowed through.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from mcpforge.indexing.parser import parse_source
from mcpforge.models.index import (
    FileKind,
    FileNode,
    FrameworkInfo,
    RepositoryIndex,
    SymbolKind,
)
from mcpforge.security.pipeline import FilterResult, filter_tree

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

#: MVP support is Next.js + React + TypeScript, and the product says so plainly
#: rather than producing plausible output for a stack it cannot reason about.
SUPPORTED_FRAMEWORK = "next.js"


def detect_router(paths: set[str]) -> str | None:
    """App Router vs Pages Router, from the tree rather than from a guess."""
    has_app = any(p.startswith(("app/", "src/app/")) for p in paths)
    has_pages = any(p.startswith(("pages/", "src/pages/")) for p in paths)
    if has_app and has_pages:
        return "app+pages"
    if has_app:
        return "app"
    if has_pages:
        return "pages"
    return None


#: Lockfile name -> the package manager that writes it.
PACKAGE_MANAGERS = {
    "package-lock.json": "npm",
    "yarn.lock": "yarn",
    "pnpm-lock.yaml": "pnpm",
    "bun.lockb": "bun",
}


def detect_package_manager(lockfiles: list[str]) -> str | None:
    for lockfile in lockfiles:
        manager = PACKAGE_MANAGERS.get(Path(lockfile).name.lower())
        if manager:
            return manager
    return None


def detect_framework(package_json: str | None) -> FrameworkInfo:
    if package_json is None:
        return FrameworkInfo(
            name="unknown",
            supported=False,
            reason="No package.json found, so the framework could not be determined.",
        )
    try:
        data = json.loads(package_json)
    except json.JSONDecodeError:
        return FrameworkInfo(
            name="unknown", supported=False, reason="package.json is not valid JSON."
        )

    deps: dict[str, str] = {**data.get("dependencies", {}), **data.get("devDependencies", {})}

    for name, label in (
        ("next", "next.js"),
        ("nuxt", "nuxt"),
        ("@angular/core", "angular"),
        ("svelte", "svelte"),
        ("vue", "vue"),
        ("react", "react"),
    ):
        if name in deps:
            supported = label == SUPPORTED_FRAMEWORK
            return FrameworkInfo(
                name=label,
                version=deps[name].lstrip("^~"),
                supported=supported,
                reason=(
                    "Next.js is supported."
                    if supported
                    else f"MCPForge supports Next.js today. {label} is not supported yet, "
                    "so analysis stops here rather than producing output it cannot stand behind."
                ),
            )

    return FrameworkInfo(
        name="unknown",
        supported=False,
        reason="No recognised web framework in package.json.",
    )


def route_path_for(path: str) -> str | None:
    """Next.js App Router: src/app/book/page.tsx -> /book."""
    match = re.match(r"^(?:src/)?app/(.*)/(page|route)\.(tsx?|jsx?)$", path)
    if match:
        segment = match.group(1)
        # Route groups like (marketing) do not appear in the URL.
        cleaned = "/".join(p for p in segment.split("/") if not p.startswith("("))
        return f"/{cleaned}" if cleaned else "/"
    if re.match(r"^(?:src/)?app/(page|route)\.(tsx?|jsx?)$", path):
        return "/"
    return None


def classify_file(path: str, parsed_exports: list[str]) -> FileKind:
    name = Path(path).name

    if re.search(r"\.(test|spec)\.[jt]sx?$", name) or "/__tests__/" in path:
        return FileKind.TEST
    if name.endswith((".css", ".scss", ".sass")):
        return FileKind.STYLE
    if name in ("package.json", "tsconfig.json") or name.startswith("next.config"):
        return FileKind.CONFIG
    if name in ("route.ts", "route.js", "route.tsx"):
        return FileKind.API_HANDLER
    if name in ("page.tsx", "page.ts", "layout.tsx", "template.tsx", "loading.tsx", "error.tsx"):
        return FileKind.ROUTE
    if "/components/" in path or "/components/" in f"/{path}":
        return FileKind.COMPONENT
    if name in ("types.ts", "types.d.ts") or "/models/" in path:
        return FileKind.MODEL
    if "/lib/" in path or "/services/" in path or "/server/" in path:
        return FileKind.SERVICE
    return FileKind.UNKNOWN


def _resolve_import(from_path: str, spec: str, known: set[str]) -> str | None:
    """Resolve an import to a file in the repository, or None if external."""
    if spec.startswith("@/"):
        base = f"src/{spec[2:]}"
    elif spec.startswith("."):
        base = str((Path(from_path).parent / spec).as_posix())
        # Path normalisation for ../ segments.
        parts: list[str] = []
        for part in base.split("/"):
            if part == "..":
                if parts:
                    parts.pop()
            elif part not in (".", ""):
                parts.append(part)
        base = "/".join(parts)
    else:
        return None  # external package

    for suffix in (".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx"):
        candidate = f"{base}{suffix}"
        if candidate in known:
            return candidate
    return base if base in known else None


def build_index(root: Path, *, max_bytes: int = 262_144) -> RepositoryIndex:
    """Index a checked-out tree. Filtering runs first, always."""
    filtered: FilterResult = filter_tree(root, max_bytes=max_bytes)
    contents = dict(filtered.included)
    known = set(contents)

    framework = detect_framework(contents.get("package.json"))
    framework = framework.model_copy(
        update={
            "router": detect_router(known),
            "package_manager": detect_package_manager(filtered.lockfiles),
        }
    )

    files: list[FileNode] = []
    for path, text in filtered.included:
        suffix = Path(path).suffix
        if suffix not in (".ts", ".tsx", ".js", ".jsx"):
            files.append(
                FileNode(
                    path=path,
                    kind=classify_file(path, []),
                    language=suffix.lstrip(".") or "text",
                    lines=text.count("\n") + 1,
                )
            )
            continue

        parsed = parse_source(text, tsx=suffix in (".tsx", ".jsx"))
        kind = classify_file(path, parsed.exports)

        # A file under /lib/ that only defines types is a model, not a service.
        if (
            kind is FileKind.SERVICE
            and parsed.symbols
            and all(s.kind in (SymbolKind.TYPE, SymbolKind.INTERFACE) for s in parsed.symbols)
        ):
            kind = FileKind.MODEL

        node = FileNode(
            path=path,
            kind=kind,
            language=suffix.lstrip("."),
            lines=text.count("\n") + 1,
            symbols=parsed.symbols,
            imports=parsed.imports,
            exports=parsed.exports,
            jsx_elements=parsed.jsx_elements,
            call_sites=parsed.call_sites,
            route_path=route_path_for(path),
        )
        if kind is FileKind.API_HANDLER:
            node.http_methods = [
                s.name for s in parsed.symbols if s.name in HTTP_METHODS and s.exported
            ]
        files.append(node)

    graph: dict[str, list[str]] = {}
    for node in files:
        resolved = [_resolve_import(node.path, spec, known) for spec in node.imports]
        graph[node.path] = sorted({r for r in resolved if r})

    return RepositoryIndex(
        root=str(root),
        framework=framework,
        files=sorted(files, key=lambda f: f.path),
        dependency_graph=graph,
        quarantined_paths=filtered.quarantined_paths,
        excluded_count=len(filtered.excluded),
        lockfiles=filtered.lockfiles,
    )
