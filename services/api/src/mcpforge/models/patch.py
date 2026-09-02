"""Generated patch representation — ticket F5-03.

A patch is data, never a filesystem effect. Agent 3 produces one of these; the
GitHub writer in Phase 6 is the only thing that turns it into commits, and only
behind an approval whose hash covers exactly this patch.

Each file carries a plain-language rationale and the tool it serves, because
04_FRONTEND_SPEC.md §6 requires the diff view to answer "why does this file
change?" without the reader guessing.
"""

from __future__ import annotations

import difflib
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, Field, field_validator


class ChangeKind(StrEnum):
    ADD = "add"
    MODIFY = "modify"


class FileChange(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    kind: ChangeKind
    contents: str
    #: One sentence, shown beside the file in the diff view.
    rationale: str = Field(min_length=1, max_length=300)
    #: Which generated tool this file serves, for the chip in the diff header.
    affected_tool: str | None = None
    #: Present for MODIFY, so a unified diff can be produced.
    original: str | None = None

    @field_validator("path")
    @classmethod
    def _stays_inside_the_repository(cls, v: str) -> str:
        """A generated path is written into someone's repository.

        Absolute paths and `..` segments are refused here rather than caught
        later by the writer, because a patch that cannot be applied safely
        should not reach a human for approval at all.
        """
        if v.startswith("/") or v.startswith("~"):
            raise ValueError(f"{v!r} is absolute; generated paths must be repository-relative")
        parts = PurePosixPath(v).parts
        if ".." in parts:
            raise ValueError(f"{v!r} climbs out of the repository")
        if any(part in (".git", ".github") for part in parts):
            raise ValueError(f"{v!r} targets repository infrastructure")
        return v

    def unified_diff(self) -> str:
        before = (self.original or "").splitlines(keepends=True)
        after = self.contents.splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{self.path}" if self.kind is ChangeKind.MODIFY else "/dev/null",
                tofile=f"b/{self.path}",
                n=3,
            )
        )

    @property
    def added_lines(self) -> int:
        return sum(
            1
            for line in self.unified_diff().splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )

    @property
    def removed_lines(self) -> int:
        return sum(
            1
            for line in self.unified_diff().splitlines()
            if line.startswith("-") and not line.startswith("---")
        )


class GeneratedPatch(BaseModel):
    """What the generator produces. Reviewed, approved, and only then written."""

    files: list[FileChange] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=600)
    #: The commit the patch applies to. An approval is bound to this plus the
    #: patch hash, so a moved base invalidates it.
    base_commit: str | None = None

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def total_added(self) -> int:
        return sum(f.added_lines for f in self.files)

    @property
    def total_removed(self) -> int:
        return sum(f.removed_lines for f in self.files)

    def unified_diff(self) -> str:
        return "\n".join(f.unified_diff() for f in self.files)

    def hashable(self) -> dict[str, object]:
        """The stable shape an approval hash is taken over.

        Rationales are excluded: rewording an explanation must not invalidate a
        human's approval of the code itself.
        """
        return {
            "base_commit": self.base_commit,
            "files": [
                {"path": f.path, "kind": f.kind.value, "contents": f.contents}
                for f in sorted(self.files, key=lambda f: f.path)
            ],
        }
