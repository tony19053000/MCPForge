"""Branch and pull-request writer — ticket F6-02, 03_SECURITY_ACCESS.md §6.

The only component in MCPForge that can change a developer's repository. Every
other module reads. Because of that, this one is written as a list of refusals
with a small amount of work at the end:

- the project must be bound to this repository (§5)
- the project must be in `WRITE_PR` mode, elevated by a recorded human act
- both `PATCH_APPROVED` and `PR_APPROVED` approvals must exist, match this
  session, and cover the exact patch by hash
- the branch must be under `mcpforge/`, and must not exist already
- the base must not be the branch we write to, and we never write to the default
  or a protected branch
- there is no force-push code path, and no history rewrite

The GitHub API calls used here (create ref, create blob/tree/commit, create PR)
cannot delete or overwrite an existing ref, which is a second reason a protected
branch is out of reach.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from mcpforge.github.boundary import assert_may_write, assert_within_boundary
from mcpforge.github.client import ACCEPT, GitHubError, InstallationToken
from mcpforge.logging import get_logger
from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    Project,
    artifact_hash,
)
from mcpforge.models.patch import GeneratedPatch
from mcpforge.models.toolplan import ToolPlan
from mcpforge.security.policy import evaluate_policy

log = get_logger(__name__)

#: Every branch MCPForge creates lives here. Enforced, not conventional.
BRANCH_PREFIX = "mcpforge/"

#: Branch names we refuse outright even under the prefix, as a second guard.
PROTECTED_NAMES = frozenset({"main", "master", "develop", "trunk", "release", "production"})

_SLUG = re.compile(r"[^a-z0-9-]+")


class WriteRefusedError(Exception):
    """A precondition for writing was not met. Never downgraded to a warning."""


class BranchExistsError(WriteRefusedError):
    """The branch is already there. We create; we never overwrite."""


def branch_name_for(slug: str) -> str:
    """`mcpforge/webmcp-<slug>`, per §6."""
    cleaned = _SLUG.sub("-", slug.lower()).strip("-") or "integration"
    return f"{BRANCH_PREFIX}webmcp-{cleaned[:60]}"


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    branch: str


class BranchAndPullRequestWriter:
    """Creates a branch, commits a patch to it, and opens a pull request."""

    def __init__(
        self,
        *,
        token: InstallationToken,
        http: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=60)
        self._base = base_url.rstrip("/")

    # -- refusals ----------------------------------------------------------

    @staticmethod
    def assert_writable(
        *,
        project: Project,
        repository_full_name: str,
        patch: GeneratedPatch,
        plan: ToolPlan,
        patch_approval: Approval | None,
        pr_approval: Approval | None,
        session_id: str,
        default_branch: str,
        branch: str,
    ) -> None:
        """Everything that must hold before a single byte is written.

        Deliberately one function taking everything it needs, so a caller cannot
        satisfy half of it and proceed.
        """
        # 1. The project is bound to this repository, and may write at all.
        assert_within_boundary(project, repository_full_name)
        assert_may_write(project)

        # 2. The branch is ours, and is not a protected name.
        if not branch.startswith(BRANCH_PREFIX):
            raise WriteRefusedError(
                f"Refusing to write to {branch!r}: MCPForge only writes to "
                f"{BRANCH_PREFIX}* branches."
            )
        tail = branch[len(BRANCH_PREFIX) :]
        if tail.lower() in PROTECTED_NAMES or branch == default_branch:
            raise WriteRefusedError(f"Refusing to write to {branch!r}: it is a protected name.")
        if default_branch.startswith(BRANCH_PREFIX):
            # Someone has made an mcpforge/* branch their default. Stop.
            raise WriteRefusedError(
                f"The repository's default branch is {default_branch!r}, which is inside "
                "the namespace MCPForge writes to. Refusing rather than risking it."
            )

        # 3. Both approvals exist, belong to this session, and cover this patch.
        expected = artifact_hash(patch.hashable())
        for approval, gate in (
            (patch_approval, ApprovalGate.PATCH),
            (pr_approval, ApprovalGate.PULL_REQUEST),
        ):
            if approval is None:
                raise WriteRefusedError(f"No {gate.value} approval exists for this patch.")
            if approval.session_id != session_id or approval.project_id != project.id:
                raise WriteRefusedError(f"The {gate.value} approval belongs to another run.")
            if approval.gate is not gate:
                raise WriteRefusedError(f"Approval {approval.id} is not a {gate.value} approval.")
            if not approval.covers(expected):
                raise WriteRefusedError(
                    f"The {gate.value} approval does not cover this patch. It was either "
                    "not approved, or the patch changed after approval."
                )

        # 4. The policy engine still passes on the exact patch being written.
        blocking = [f for f in evaluate_policy(plan, patch) if f.severity.blocks]
        if blocking:
            rules = ", ".join(sorted({f.rule for f in blocking}))
            raise WriteRefusedError(
                f"Policy blocks this patch at write time: {rules}. Approval does not "
                "override a policy violation."
            )

    # -- the work ----------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        response = await self._http.request(
            method,
            f"{self._base}{path}",
            headers={"Authorization": f"Bearer {self._token.token}", "Accept": ACCEPT},
            **kwargs,  # type: ignore[arg-type]
        )
        if response.status_code >= 400:
            # The token never appears in the message.
            raise GitHubError(
                f"{method} {path} failed: {response.status_code}",
                status=response.status_code,
            )
        return response

    async def branch_exists(self, repository: str, branch: str) -> bool:
        response = await self._http.get(
            f"{self._base}/repos/{repository}/git/ref/heads/{branch}",
            headers={"Authorization": f"Bearer {self._token.token}", "Accept": ACCEPT},
        )
        return response.status_code == 200

    async def create_pull_request(
        self,
        *,
        project: Project,
        repository_full_name: str,
        default_branch: str,
        base_commit: str,
        branch: str,
        patch: GeneratedPatch,
        plan: ToolPlan,
        patch_approval: Approval | None,
        pr_approval: Approval | None,
        session_id: str,
        title: str,
        body: str,
    ) -> PullRequest:
        """Create the branch, commit the patch, open the pull request."""
        self.assert_writable(
            project=project,
            repository_full_name=repository_full_name,
            patch=patch,
            plan=plan,
            patch_approval=patch_approval,
            pr_approval=pr_approval,
            session_id=session_id,
            default_branch=default_branch,
            branch=branch,
        )

        if await self.branch_exists(repository_full_name, branch):
            raise BranchExistsError(
                f"{branch} already exists. MCPForge creates branches; it never overwrites "
                "one, and it never force-pushes."
            )

        repo = repository_full_name

        # Blobs, then a tree on top of the base commit's tree, then a commit.
        tree_entries = []
        for change in patch.files:
            blob = await self._request(
                "POST",
                f"/repos/{repo}/git/blobs",
                json={"content": change.contents, "encoding": "utf-8"},
            )
            tree_entries.append(
                {
                    "path": change.path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob.json()["sha"],
                }
            )

        tree = await self._request(
            "POST",
            f"/repos/{repo}/git/trees",
            json={"base_tree": base_commit, "tree": tree_entries},
        )
        commit = await self._request(
            "POST",
            f"/repos/{repo}/git/commits",
            json={
                "message": title,
                "tree": tree.json()["sha"],
                "parents": [base_commit],
            },
        )

        # create-ref, not update-ref: it fails if the branch exists and cannot
        # move an existing one. There is no force flag anywhere in this module.
        await self._request(
            "POST",
            f"/repos/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": commit.json()["sha"]},
        )
        log.info("github.branch_created", repository=repo, branch=branch)

        pull = await self._request(
            "POST",
            f"/repos/{repo}/pulls",
            json={"title": title, "body": body, "head": branch, "base": default_branch},
        )
        data = pull.json()
        log.info("github.pull_request_opened", repository=repo, number=data["number"])
        return PullRequest(number=data["number"], url=data["html_url"], branch=branch)

    async def aclose(self) -> None:
        await self._http.aclose()
