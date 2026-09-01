"""The filtering pipeline — 03_SECURITY_ACCESS.md §4.1.

One entry point, so ordering cannot be got wrong by a caller. A file is only
ever readable through `filter_tree`, and `filter_tree` decides on the path
before it opens anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from mcpforge.security.filters import (
    FileDecision,
    Verdict,
    classify_content,
    classify_path,
)


@dataclass
class FilterResult:
    """What survived, and what did not.

    `quarantined` holds paths and rule names only — never contents. It is safe
    to show a user and safe to persist.
    """

    included: list[tuple[str, str]] = field(default_factory=list)
    quarantined: list[FileDecision] = field(default_factory=list)
    excluded: list[FileDecision] = field(default_factory=list)
    lockfiles: list[str] = field(default_factory=list)

    @property
    def included_paths(self) -> list[str]:
        return [path for path, _ in self.included]

    @property
    def quarantined_paths(self) -> list[str]:
        return [d.path for d in self.quarantined]


def _walk(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield path


def filter_tree(root: Path, *, max_bytes: int = 262_144) -> FilterResult:
    """Filter a checked-out tree.

    The only way to obtain file contents for indexing. A quarantined-by-path
    file is never opened, so its bytes never enter the process.
    """
    result = FilterResult()
    root = root.resolve()

    for absolute in _walk(root):
        try:
            relative = absolute.relative_to(root).as_posix()
        except ValueError:
            # Outside the root. Cannot happen via rglob, but never trust that.
            continue

        try:
            size = absolute.stat().st_size
        except OSError:
            continue

        decision = classify_path(relative, size_bytes=size, max_bytes=max_bytes)

        if decision.verdict is Verdict.EXCLUDED_LOCKFILE:
            result.lockfiles.append(relative)
            result.excluded.append(decision)
            continue

        if decision.verdict.is_secret:
            # Deliberately not opened.
            result.quarantined.append(decision)
            continue

        if not decision.included:
            result.excluded.append(decision)
            continue

        try:
            text = absolute.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            result.excluded.append(
                FileDecision(relative, Verdict.EXCLUDED_BINARY, "not valid UTF-8 text")
            )
            continue

        content_decision = classify_content(relative, text)
        if content_decision.verdict.is_secret:
            result.quarantined.append(content_decision)
            continue

        result.included.append((relative, text))

    return result
