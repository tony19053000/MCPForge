"""Context retrieval — 02_ARCHITECTURE.md §5, ticket F3-06.

The last gate before prompt construction. Given an agent step and a token
budget, rank and slice only the snippets that step actually needs.

Two rules:

1. A quarantined file is never selectable. The index does not carry its content,
   and this module refuses it by path as well — defence in depth behind F3-03.
2. Exceeding the budget with *required* evidence is a loud failure, never a
   silent truncation. An agent reasoning over quietly-cut context produces
   confident nonsense, which is worse than an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mcpforge.models.index import FileKind, RepositoryIndex

#: Rough characters-per-token. Deliberately conservative: overestimating the
#: budget is how context gets silently truncated downstream.
CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


class BudgetExceededError(Exception):
    """Required evidence does not fit. Raised rather than trimmed."""

    def __init__(self, needed: int, budget: int, dropped: list[str]) -> None:
        super().__init__(
            f"Required context needs ~{needed} tokens but the budget is {budget}. "
            f"Could not include: {', '.join(dropped)}"
        )
        self.needed = needed
        self.budget = budget
        self.dropped = dropped


class QuarantinedFileError(Exception):
    """Something asked for a file the secret filter rejected."""


@dataclass(frozen=True)
class Snippet:
    path: str
    text: str
    reason: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass
class RetrievalRequest:
    """What a step needs. `required` must fit or the call fails."""

    #: Paths that must be included. Missing any is an error, not a warning.
    required: list[str] = field(default_factory=list)
    #: Kinds to fill remaining budget with, in priority order.
    preferred_kinds: list[FileKind] = field(default_factory=list)
    #: Symbol names whose defining files should be pulled in.
    symbols: list[str] = field(default_factory=list)
    token_budget: int = 30_000


@dataclass
class RetrievedContext:
    snippets: list[Snippet]
    total_tokens: int
    budget: int
    omitted: list[str]

    def render(self) -> str:
        """The text an agent sees. Each snippet is labelled with its path so a
        claim can be traced back to a file."""
        parts = []
        for snippet in self.snippets:
            parts.append(f"--- {snippet.path} ({snippet.reason}) ---\n{snippet.text}")
        return "\n\n".join(parts)


class ContextRetriever:
    """Reads file contents from the workspace, guarded by the index.

    Content comes from disk rather than the index, because the index
    deliberately stores no bodies. Every read is checked against the index's
    quarantine list first.
    """

    def __init__(self, index: RepositoryIndex, root: Path) -> None:
        self._index = index
        self._root = root.resolve()
        self._quarantined = set(index.quarantined_paths)
        self._known = {f.path for f in index.files}

    def _read(self, path: str) -> str:
        if path in self._quarantined:
            raise QuarantinedFileError(
                f"{path} was quarantined by the secret filter and can never be retrieved"
            )
        if path not in self._known:
            raise FileNotFoundError(f"{path} is not in the index")

        resolved = (self._root / path).resolve()
        if not resolved.is_relative_to(self._root):
            raise QuarantinedFileError(f"{path} resolves outside the repository")
        return resolved.read_text(encoding="utf-8")

    def _files_for_symbols(self, names: list[str]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for name in names:
            found = self._index.find_symbol(name)
            if found is not None:
                file, symbol = found
                out.append((file.path, f"defines {symbol.name} at line {symbol.line}"))
        return out

    def retrieve(self, request: RetrievalRequest) -> RetrievedContext:
        selected: dict[str, Snippet] = {}
        used = 0

        # 1. Required. These must fit, or we fail loudly.
        dropped: list[str] = []
        for path in request.required:
            snippet = Snippet(path, self._read(path), "required")
            if used + snippet.tokens > request.token_budget:
                dropped.append(path)
            else:
                selected[path] = snippet
                used += snippet.tokens
        if dropped:
            raise BudgetExceededError(
                needed=used + sum(estimate_tokens(self._read(p)) for p in dropped),
                budget=request.token_budget,
                dropped=dropped,
            )

        # 2. Symbol definitions the step named.
        omitted: list[str] = []
        for path, reason in self._files_for_symbols(request.symbols):
            if path in selected:
                continue
            snippet = Snippet(path, self._read(path), reason)
            if used + snippet.tokens > request.token_budget:
                omitted.append(path)
                continue
            selected[path] = snippet
            used += snippet.tokens

        # 3. Fill the rest by kind, in the order the step asked for.
        for kind in request.preferred_kinds:
            for file in self._index.by_kind(kind):
                if file.path in selected or file.path in self._quarantined:
                    continue
                snippet = Snippet(file.path, self._read(file.path), f"{kind.value} file")
                if used + snippet.tokens > request.token_budget:
                    omitted.append(file.path)
                    continue
                selected[file.path] = snippet
                used += snippet.tokens

        return RetrievedContext(
            snippets=list(selected.values()),
            total_tokens=used,
            budget=request.token_budget,
            omitted=omitted,
        )
