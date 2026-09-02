"""The adapter registry.

The supported-framework list the product shows is computed from what is
registered here. There is no second list to drift out of date.
"""

from __future__ import annotations

from mcpforge.generation.adapters.base import (
    AdapterInfo,
    FrameworkAdapter,
    UnsupportedFrameworkError,
)
from mcpforge.generation.nextjs import generate_patch
from mcpforge.models.index import RepositoryIndex
from mcpforge.models.patch import GeneratedPatch
from mcpforge.models.webmcp import WebMCPToolset


class NextJsAdapter:
    """Next.js App Router — the one framework MCPForge actually supports."""

    @property
    def info(self) -> AdapterInfo:
        return AdapterInfo(framework="next.js", display_name="Next.js (App Router)")

    def supports(self, index: RepositoryIndex) -> bool:
        return index.framework.name == "next.js" and index.framework.supported

    def generate(self, toolset: WebMCPToolset, *, base_commit: str | None = None) -> GeneratedPatch:
        return generate_patch(toolset, base_commit=base_commit)


ADAPTERS: tuple[FrameworkAdapter, ...] = (NextJsAdapter(),)


def supported_frameworks() -> list[str]:
    """What the UI shows. Generated from the registry, never hardcoded copy."""
    return [adapter.info.display_name for adapter in ADAPTERS]


def adapter_for(index: RepositoryIndex) -> FrameworkAdapter:
    """The adapter for this repository, or a refusal naming what was found."""
    for adapter in ADAPTERS:
        if adapter.supports(index):
            return adapter
    raise UnsupportedFrameworkError(detected=index.framework.name, supported=supported_frameworks())
