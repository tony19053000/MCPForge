"""Branch naming and the shapes MCPForge will write.

Its own module because both the writer and the failure-recovery code need these
rules, and neither should import the other.
"""

from __future__ import annotations

import re

#: Every branch MCPForge creates lives here. Enforced, not conventional.
BRANCH_PREFIX = "mcpforge/"

#: Branch names refused outright even under the prefix, as a second guard.
PROTECTED_NAMES = frozenset({"main", "master", "develop", "trunk", "release", "production"})

#: The only branch shape MCPForge writes. Matched in full rather than by prefix:
#: httpx collapses dot segments, so `mcpforge/../../../other` passes a prefix
#: test and silently retargets the request to an unrelated endpoint.
BRANCH_SHAPE = re.compile(r"^mcpforge/webmcp-[a-z0-9][a-z0-9-]{0,59}$")

_SLUG = re.compile(r"[^a-z0-9-]+")


def branch_name_for(slug: str) -> str:
    """`mcpforge/webmcp-<slug>`, per 03_SECURITY_ACCESS.md §6."""
    cleaned = _SLUG.sub("-", slug.lower()).strip("-") or "integration"
    return f"{BRANCH_PREFIX}webmcp-{cleaned[:60]}"
