"""Index a real repository end to end: GitHub -> sandbox -> filter -> index.

Proves the whole Phase 3 chain against the owner's real repository.

    uv run python scripts/index_check.py [owner/repo]
"""

from __future__ import annotations

import asyncio
import sys

from mcpforge.config import get_settings
from mcpforge.execution.development import DevelopmentSecureExecutor
from mcpforge.execution.provider import Command, WorkspaceSpec
from mcpforge.github.client import GitHubAppClient
from mcpforge.indexing.indexer import build_index


async def main(full_name: str | None) -> int:
    settings = get_settings()
    client = GitHubAppClient(
        app_id=settings.github_app_id,
        private_key_path=settings.github_app_private_key_path,
    )
    if not client.configured:
        print("GitHub is not configured.")
        return 2

    installations = await client.list_installations()
    if not installations:
        print("The App is installed nowhere. Install it on a repository first.")
        return 2

    token = await client.create_installation_token(installations[0].id)
    repos = await client.list_repositories(token)
    if not repos:
        print("No repositories are reachable through this installation.")
        return 2

    target = next((r for r in repos if r.full_name == full_name), repos[0])
    print(f"repository : {target.full_name} (branch {target.default_branch})")

    executor = DevelopmentSecureExecutor()
    workspace = await executor.create_workspace(WorkspaceSpec(run_id="index-check"))
    print(f"workspace  : {workspace.root}")
    print(f"trust      : {workspace.trust_level.value}")

    try:
        # The one networked step. The token is used here and nowhere else.
        clone_url = f"https://x-access-token:{token.token}@github.com/{target.full_name}.git"
        result = await executor.run(
            workspace,
            Command(
                argv=(
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    target.default_branch,
                    clone_url,
                    "repo",
                ),
                timeout_seconds=120,
            ),
        )
        if not result.ok:
            # Never print stderr raw: it can echo the tokenised clone URL.
            print(f"clone failed with exit code {result.exit_code}")
            return 1
        print("clone      : ok")

        index = build_index(workspace.root / "repo")
        print(f"\nframework  : {index.framework.name} — supported: {index.framework.supported}")
        print(f"             {index.framework.reason}")
        print(
            f"files      : {len(index.files)} indexed, {index.excluded_count} excluded, "
            f"{len(index.quarantined_paths)} quarantined"
        )
        if index.quarantined_paths:
            print("quarantined:")
            for path in index.quarantined_paths:
                print(f"  - {path}")
        for label, nodes in (
            ("routes", index.routes),
            ("api handlers", index.api_handlers),
            ("services", index.services),
        ):
            print(f"{label}: {len(nodes)}")
            for node in nodes[:5]:
                print(f"  - {node.path}")
    finally:
        await executor.destroy(workspace)
        await client.aclose()
        print(f"\nworkspace destroyed: {not workspace.root.exists()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None)))
