"""Framework adapters — ticket F5-04, 01_PRD.md §9.

MCPForge supports Next.js. It must say so rather than producing plausible output
for a stack it cannot reason about, so the supported list is derived from the
registered adapters and never written as marketing copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mcpforge.models.index import RepositoryIndex
from mcpforge.models.patch import GeneratedPatch
from mcpforge.models.webmcp import WebMCPToolset


class UnsupportedFrameworkError(Exception):
    """The repository's framework has no adapter.

    Carries the detected name so the product can say what it found, rather than
    failing vaguely (01_PRD.md §9).
    """

    def __init__(self, detected: str, supported: list[str]) -> None:
        super().__init__(
            f"MCPForge supports {', '.join(supported)} today. This repository uses "
            f"{detected}, so analysis stops here rather than producing output it "
            "cannot stand behind."
        )
        self.detected = detected
        self.supported = supported


@dataclass(frozen=True)
class AdapterInfo:
    framework: str
    display_name: str


class FrameworkAdapter(Protocol):
    @property
    def info(self) -> AdapterInfo: ...

    def supports(self, index: RepositoryIndex) -> bool: ...

    def generate(
        self, toolset: WebMCPToolset, *, base_commit: str | None = None
    ) -> GeneratedPatch: ...
