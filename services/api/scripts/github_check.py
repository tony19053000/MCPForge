"""Live GitHub App check — run manually.

Verifies the App credentials work and reports exactly what access exists.
Prints no token material.

    uv run python scripts/github_check.py
"""

from __future__ import annotations

import asyncio
import sys

from mcpforge.config import get_settings
from mcpforge.github.client import GitHubAppClient, GitHubNotConfiguredError


async def main() -> int:
    settings = get_settings()
    client = GitHubAppClient(
        app_id=settings.github_app_id,
        private_key_path=settings.github_app_private_key_path,
    )

    if not client.configured:
        print("GitHub is not configured. Set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH.")
        return 2

    print(f"app id : {settings.github_app_id}")
    try:
        installations = await client.list_installations()
    except GitHubNotConfiguredError as exc:
        print(f"not configured: {exc}")
        return 2

    if not installations:
        print("\nThe App exists but is installed nowhere.")
        print("Install it on a repository:")
        print("  GitHub App page -> Install App -> Only select repositories")
        print("\nThis is correct behaviour, not a failure: no installation means no access.")
        await client.aclose()
        return 0

    for installation in installations:
        scope = (
            "selected repositories only"
            if installation.is_scoped_to_selected_repositories
            else "ALL repositories on the account"
        )
        print(f"\ninstallation {installation.id} on {installation.account} — {scope}")
        if not installation.is_scoped_to_selected_repositories:
            print("  WARNING: account-wide access is broader than MCPForge should hold.")

        token = await client.create_installation_token(installation.id)
        print(f"  token minted, expires {token.expires_at.isoformat()}")

        repos = await client.list_repositories(token)
        print(f"  {len(repos)} repository(ies) reachable:")
        for repo in repos:
            visibility = "private" if repo.private else "public"
            print(f"    - {repo.full_name}  ({visibility}, default branch: {repo.default_branch})")

    await client.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
