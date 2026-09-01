"""Repository index types — 02_ARCHITECTURE.md §5.

The index holds **structure, not source**. It records where things are and how
they relate, so a later step can fetch only the few snippets an agent actually
needs. Nothing here stores a file body.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class FileKind(StrEnum):
    ROUTE = "route"
    API_HANDLER = "api-handler"
    COMPONENT = "component"
    SERVICE = "service"
    MODEL = "model"
    CONFIG = "config"
    TEST = "test"
    STYLE = "style"
    UNKNOWN = "unknown"


class SymbolKind(StrEnum):
    FUNCTION = "function"
    CLASS = "class"
    CONST = "const"
    TYPE = "type"
    INTERFACE = "interface"
    COMPONENT = "component"


class Symbol(BaseModel):
    """A named thing in a file. Carries a signature, never a body."""

    name: str
    kind: SymbolKind
    line: int
    end_line: int
    exported: bool = False
    is_async: bool = False
    #: Parameter names only. Enough to design a tool schema, not enough to leak logic.
    params: list[str] = Field(default_factory=list)


class CallSite(BaseModel):
    """A frontend → backend call, so the graph can join a form to its handler."""

    line: int
    method: str
    url: str


class FileNode(BaseModel):
    path: str
    kind: FileKind
    language: str
    lines: int
    symbols: list[Symbol] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)
    #: JSX elements used, which is how components are recognised as components.
    jsx_elements: list[str] = Field(default_factory=list)
    call_sites: list[CallSite] = Field(default_factory=list)
    #: Route path, for files that are routes or API handlers.
    route_path: str | None = None
    http_methods: list[str] = Field(default_factory=list)


class FrameworkInfo(BaseModel):
    """What MCPForge detected. `supported` gates the whole pipeline."""

    name: str
    version: str | None = None
    router: str | None = None
    package_manager: str | None = None
    supported: bool = False
    reason: str = ""


class RepositoryIndex(BaseModel):
    """The whole picture, with no file contents in it."""

    root: str
    framework: FrameworkInfo
    files: list[FileNode] = Field(default_factory=list)
    #: path -> paths it imports, resolved within the repository.
    dependency_graph: dict[str, list[str]] = Field(default_factory=dict)
    quarantined_paths: list[str] = Field(default_factory=list)
    excluded_count: int = 0
    lockfiles: list[str] = Field(default_factory=list)

    def by_kind(self, kind: FileKind) -> list[FileNode]:
        return [f for f in self.files if f.kind is kind]

    def find_symbol(self, name: str) -> tuple[FileNode, Symbol] | None:
        """Resolve a symbol name to where it is defined.

        Used to check that an agent's claim points at something that exists.
        """
        for file in self.files:
            for symbol in file.symbols:
                if symbol.name == name:
                    return file, symbol
        return None

    @property
    def routes(self) -> list[FileNode]:
        return self.by_kind(FileKind.ROUTE)

    @property
    def api_handlers(self) -> list[FileNode]:
        return self.by_kind(FileKind.API_HANDLER)

    @property
    def services(self) -> list[FileNode]:
        return self.by_kind(FileKind.SERVICE)
