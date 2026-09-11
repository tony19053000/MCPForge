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


class TsType(StrEnum):
    """What the parser can say, precisely, about a declared TypeScript type.

    Syntax only — tree-sitter, no type checker. Only a type whose meaning is
    fixed by its syntax is classified: `string`, `number`, `boolean`, `T[]`, an
    inline `{ ... }`, and `any`/`unknown` (which accept anything). Everything
    else — a union, a generic, a named alias or interface, a literal type, a
    missing annotation — is `UNKNOWN`, because resolving it would mean guessing.
    """

    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"
    #: `any` or `unknown`: every value is assignable to it.
    ANY = "any"
    UNKNOWN = "unknown"


class TypedField(BaseModel):
    """One property of an inline object-literal type. A kind, never source text."""

    name: str
    ts_type: TsType = TsType.UNKNOWN
    optional: bool = False


class TypedParameter(BaseModel):
    """One declared parameter, as far as its syntax says."""

    name: str
    ts_type: TsType = TsType.UNKNOWN
    #: Declared with `?` or given a default value, so a call may leave it out.
    optional: bool = False
    #: The properties of an inline object-literal type, when the whole shape is
    #: known. `None` when it is not — a named type, an index signature, a
    #: method member — so an absent list never reads as "has no fields".
    fields: list[TypedField] | None = None


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
    #: The subset of `params` declared with an object-literal type or as a
    #: destructuring pattern — `searchRooms(params: { guests: number })`. Syntax
    #: only, from the declaration: it is what tells a single-object call
    #: (`fn({ a, b })`) apart from a positional one (`fn(a, b)`) without a model
    #: guessing. See `orchestration/toolset.py`.
    object_params: list[str] = Field(default_factory=list)
    #: Each parameter's type kind, optionality and — for an inline object —
    #: fields, aligned with `params`. What lets a binding be refused when the
    #: plan's types or fields do not fit the real declaration (the first live
    #: F9-01 analysis run generated a call `tsc` rejected). Empty for an index
    #: persisted before this existed; the binding then records every type as
    #: not checked rather than assuming one.
    signature: list[TypedParameter] = Field(default_factory=list)


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
