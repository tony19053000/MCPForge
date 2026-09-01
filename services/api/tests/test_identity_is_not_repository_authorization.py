"""MCPForge identity is separate from GitHub repository authorization.

03_SECURITY_ACCESS.md §6: signing into MCPForge grants identity, not repository
access. These are distinct records and distinct checks. F1-06 requires this be
asserted by test, so the separation cannot erode quietly as GitHub support lands
in Phase 3.
"""

from __future__ import annotations

import pathlib

from mcpforge.auth.identity import VerifiedIdentity

AUTH = pathlib.Path(__file__).resolve().parents[1] / "src" / "mcpforge" / "auth"


def test_verified_identity_carries_no_repository_authority() -> None:
    """An identity says who someone is. It never says what repository they may touch."""
    forbidden = {
        "repository",
        "repo",
        "repositories",
        "installation_id",
        "github_token",
        "access_mode",
        "scopes",
        "permissions",
    }
    fields = set(VerifiedIdentity.model_fields)
    assert not (fields & forbidden), (
        f"VerifiedIdentity must not carry repository authority: {fields & forbidden}"
    )


def test_identity_claims_cannot_grant_repository_access() -> None:
    """A token claim is attacker-influenced input, not an authorization decision.

    Even a token asserting repository permissions yields an identity with none;
    repository authorization is a separate record checked by separate code.
    """
    identity = VerifiedIdentity(
        subject="user-1",
        issuer="https://securetoken.google.com/p",
        claims={"repositories": ["victim/private"], "access_mode": "WRITE_PR"},
    )
    assert not hasattr(identity, "repositories")
    assert not hasattr(identity, "access_mode")
    # The raw claim is retained for debugging, but nothing reads authority from it.
    assert identity.claims["access_mode"] == "WRITE_PR"


def test_the_auth_module_knows_nothing_about_github() -> None:
    """No coupling between sign-in and repository access, enforced structurally."""
    offenders: list[str] = []
    for path in AUTH.rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0].lower()
            if "github" in code or "installation" in code:
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "Authentication must not reference repository access:\n" + "\n".join(
        offenders
    )
