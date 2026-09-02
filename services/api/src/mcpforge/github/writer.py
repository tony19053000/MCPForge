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

from dataclasses import dataclass

import httpx

from mcpforge.github.boundary import assert_may_write, assert_within_boundary
from mcpforge.github.branches import (
    BRANCH_PREFIX,
    BRANCH_SHAPE,
    PROTECTED_NAMES,
)
from mcpforge.github.client import ACCEPT, GitHubError, InstallationToken
from mcpforge.github.pr_description import default_title, describe_patch
from mcpforge.logging import get_logger
from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    Project,
    artifact_hash,
)
from mcpforge.models.patch import GeneratedPatch
from mcpforge.models.toolplan import ToolPlan
from mcpforge.orchestration.recovery import (
    WriteOutcome,
    WriteStage,
    may_delete_branch,
    outcome_for,
)
from mcpforge.security.filters import scan_content
from mcpforge.security.policy import evaluate_policy

log = get_logger(__name__)


class WriteRefusedError(Exception):
    """A precondition for writing was not met. Never downgraded to a warning."""


class BranchExistsError(WriteRefusedError):
    """The branch is already there. We create; we never overwrite."""


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
        base_commit: str,
    ) -> None:
        """Everything that must hold before a single byte is written.

        Deliberately one function taking everything it needs, so a caller cannot
        satisfy half of it and proceed.
        """
        # 1. The project is bound to this repository, and may write at all.
        assert_within_boundary(project, repository_full_name)
        assert_may_write(project)

        # 2. The branch is exactly the shape we generate. A prefix check is not
        #    enough: httpx collapses dot segments, so `mcpforge/../../../other`
        #    would pass one and silently retarget the request.
        if not BRANCH_SHAPE.match(branch):
            raise WriteRefusedError(
                f"Refusing to write to {branch!r}: MCPForge writes only to branches of "
                f"the form {BRANCH_PREFIX}webmcp-<slug>."
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

        # 3. The base being written to is the base the human reviewed against.
        #    `hashable()` includes base_commit, so an approval covers a patch on
        #    a specific base; committing the same files onto a different base
        #    would otherwise pass every other check.
        if patch.base_commit is None:
            raise WriteRefusedError(
                "The patch records no base commit, so an approval cannot bind the base "
                "it was reviewed against."
            )
        if base_commit != patch.base_commit:
            raise WriteRefusedError(
                f"The patch was reviewed against {patch.base_commit}, but the write "
                f"targets {base_commit}. The base moved after approval."
            )

        # 4. Both approvals exist, belong to this session, and cover this patch.
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

        # 5. The policy engine still passes on the exact patch being written.
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
        title: str | None = None,
        body: str | None = None,
    ) -> WriteOutcome:
        """Create the branch, commit the patch, open the pull request.

        Returns a `WriteOutcome` rather than raising on a partial failure: the
        developer needs to know *how far it got*, because "nothing happened" and
        "a branch exists but no pull request" call for different actions.

        A precondition failure still raises, because nothing has been written
        and there is no state to explain.
        """
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
            base_commit=base_commit,
        )

        # The description is built from the plan and patch, not accepted from a
        # caller, and scanned before it leaves — §4.4 requires the outbound scan
        # again before pull-request creation, and the body is outbound content.
        pr_title = title or default_title(plan)
        pr_body = body or describe_patch(plan, patch, branch=branch, base_commit=base_commit)
        for label, text in (("title", pr_title), ("body", pr_body)):
            hits = scan_content(text)
            if hits:
                raise WriteRefusedError(
                    f"The pull request {label} contains credential-shaped content: "
                    f"{', '.join(sorted({h.rule for h in hits}))}."
                )

        if await self.branch_exists(repository_full_name, branch):
            raise BranchExistsError(
                f"{branch} already exists. MCPForge creates branches; it never overwrites "
                "one, and it never force-pushes."
            )

        repo = repository_full_name
        stage = WriteStage.NOTHING_DONE
        branch_is_ours = False

        try:
            # The tree we build on must be a tree SHA, not a commit SHA.
            commit_object = await self._request("GET", f"/repos/{repo}/git/commits/{base_commit}")
            base_tree = commit_object.json()["tree"]["sha"]

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
                json={"base_tree": base_tree, "tree": tree_entries},
            )
            commit = await self._request(
                "POST",
                f"/repos/{repo}/git/commits",
                json={
                    "message": pr_title,
                    "tree": tree.json()["sha"],
                    "parents": [base_commit],
                },
            )
            stage = WriteStage.COMMIT_CREATED

            # create-ref, not update-ref: it fails if the branch exists and
            # cannot move an existing one. There is no force flag in this module.
            await self._request(
                "POST",
                f"/repos/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": commit.json()["sha"]},
            )
            stage = WriteStage.BRANCH_CREATED
            branch_is_ours = True
            log.info("github.branch_created", repository=repo, branch=branch)

            pull = await self._request(
                "POST",
                f"/repos/{repo}/pulls",
                json={
                    "title": pr_title,
                    "body": pr_body,
                    "head": branch,
                    "base": default_branch,
                },
            )
        except GitHubError as exc:
            cleaned = False
            if stage is WriteStage.BRANCH_CREATED:
                cleaned = await self._try_cleanup(repo, branch, created_by_this_run=branch_is_ours)
            return outcome_for(stage, branch=branch, error=exc, cleanup_performed=cleaned)

        data = pull.json()
        log.info("github.pull_request_opened", repository=repo, number=data["number"])
        return outcome_for(
            WriteStage.PULL_REQUEST_OPENED,
            branch=branch,
            pull_request=PullRequest(number=data["number"], url=data["html_url"], branch=branch),
        )

    async def _try_cleanup(self, repo: str, branch: str, *, created_by_this_run: bool) -> bool:
        """Remove a branch we created, if cleanup is permitted.

        `may_delete_branch` requires both that the branch is in our namespace and
        that this run created it. A developer may have their own `mcpforge/`
        branch, and matching a pattern is not permission to delete it.
        """
        if not may_delete_branch(branch, created_by_this_run=created_by_this_run):
            return False
        response = await self._http.request(
            "DELETE",
            f"{self._base}/repos/{repo}/git/refs/heads/{branch}",
            headers={"Authorization": f"Bearer {self._token.token}", "Accept": ACCEPT},
        )
        removed = response.status_code in (204, 200)
        log.info("github.cleanup", repository=repo, branch=branch, removed=removed)
        return removed

    async def aclose(self) -> None:
        await self._http.aclose()
