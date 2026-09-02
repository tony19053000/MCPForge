"""Branch and pull-request writer — F6-02, the T3 control.

This is the only code in MCPForge that can change someone's repository, so the
tests are mostly refusals. They must all pass before the writer is ever pointed
at a real credential.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mcpforge.generation.nextjs import generate_patch
from mcpforge.github.boundary import (
    AccessModeError,
    BoundaryError,
    NoRepositoryBoundError,
    bind_repository,
    elevate_to_write,
)
from mcpforge.github.client import InstallationToken
from mcpforge.github.writer import (
    BRANCH_PREFIX,
    BranchAndPullRequestWriter,
    BranchExistsError,
    WriteRefusedError,
    branch_name_for,
)
from mcpforge.models.core import (
    Approval,
    ApprovalGate,
    ApprovalStatus,
    Project,
    artifact_hash,
    utcnow,
)
from mcpforge.models.toolplan import ToolPlan
from mcpforge.models.webmcp import WebMCPToolset
from tests.structure import SRC, code_lines
from tests.test_generation import destructive_tool, read_tool

REPO = "tony19053000/mcpforge-test"
SESSION = "sess_1"
OWNER = "uid-owner"


@pytest.fixture
def token() -> InstallationToken:
    from datetime import timedelta

    return InstallationToken(token="ghs_x", expires_at=utcnow() + timedelta(hours=1))


@pytest.fixture
def project() -> Project:
    bound = bind_repository(
        Project(id="proj_1", owner_uid=OWNER, name="hotel"),
        repository_id="12345",
        full_name=REPO,
        base_branch="main",
    )
    return elevate_to_write(bound, actor_uid=OWNER)


@pytest.fixture
def patch() -> Any:
    return generate_patch(
        WebMCPToolset(tools=[read_tool(), destructive_tool()]), base_commit="basesha"
    )


@pytest.fixture
def plan() -> ToolPlan:
    """A plan matching the patch, so policy rules line up."""
    return ToolPlan.model_validate(
        {
            "tools": [
                {
                    "name": "search_rooms",
                    "title": "Search rooms",
                    "description": "Find rooms.",
                    "workflow_id": "search",
                    "maps_to_function": "searchRooms",
                    "parameters": [
                        {"name": "guests", "json_type": "integer", "description": "How many"}
                    ],
                    "output_description": "Rooms.",
                    "risk": "READ",
                    "evidence": [{"path": "src/lib/rooms.ts"}],
                    "approval_required": False,
                },
                {
                    "name": "cancel_reservation",
                    "title": "Cancel a reservation",
                    "description": "Cancel a booking.",
                    "workflow_id": "cancel",
                    "maps_to_function": "cancelReservation",
                    "parameters": [
                        {"name": "reservationId", "json_type": "string", "description": "Id"}
                    ],
                    "output_description": "Cancelled.",
                    "risk": "DESTRUCTIVE",
                    "evidence": [{"path": "src/lib/reservations.ts"}],
                    "approval_required": True,
                },
            ],
            "notes": [],
        }
    )


def approval(gate: ApprovalGate, patch: Any, **over: Any) -> Approval:
    data: dict[str, Any] = {
        "project_id": "proj_1",
        "session_id": SESSION,
        "gate": gate,
        "artifact_hash": artifact_hash(patch.hashable()),
        "summary": "x",
        "status": ApprovalStatus.APPROVED,
        "actor_uid": OWNER,
        "decided_at": utcnow(),
    }
    data.update(over)
    return Approval.model_validate(data)


def check(project: Project, patch: Any, plan: ToolPlan, **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "project": project,
        "repository_full_name": REPO,
        "patch": patch,
        "plan": plan,
        "patch_approval": approval(ApprovalGate.PATCH, patch),
        "pr_approval": approval(ApprovalGate.PULL_REQUEST, patch),
        "session_id": SESSION,
        "default_branch": "main",
        "branch": branch_name_for("booking"),
    }
    kwargs.update(over)
    BranchAndPullRequestWriter.assert_writable(**kwargs)


# -- the happy path, so the refusals below mean something -------------------


def test_a_fully_approved_patch_on_an_mcpforge_branch_is_allowed(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    check(project, patch, plan)


def test_the_branch_name_is_namespaced(project: Project) -> None:
    assert branch_name_for("Booking Workflow!").startswith(BRANCH_PREFIX)
    assert branch_name_for("Booking Workflow!") == "mcpforge/webmcp-booking-workflow"
    assert branch_name_for("") == "mcpforge/webmcp-integration"


# -- the default branch is unreachable, which is the whole point ------------


@pytest.mark.parametrize("branch", ["main", "master", "develop", "production", "release"])
def test_writing_to_a_bare_branch_name_is_refused(
    project: Project, patch: Any, plan: ToolPlan, branch: str
) -> None:
    with pytest.raises(WriteRefusedError, match="only writes to"):
        check(project, patch, plan, branch=branch)


@pytest.mark.parametrize("name", ["main", "master", "develop", "production"])
def test_a_protected_name_under_our_prefix_is_still_refused(
    project: Project, patch: Any, plan: ToolPlan, name: str
) -> None:
    with pytest.raises(WriteRefusedError, match="protected name"):
        check(project, patch, plan, branch=f"{BRANCH_PREFIX}{name}")


def test_writing_to_the_repositorys_actual_default_branch_is_refused(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    """Even when it is not called main."""
    with pytest.raises(WriteRefusedError):
        check(project, patch, plan, branch="trunk", default_branch="trunk")


def test_a_default_branch_inside_our_namespace_stops_everything(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    """If someone made mcpforge/* their default, our prefix guard is no longer a
    guarantee, so we refuse rather than reason about it."""
    with pytest.raises(WriteRefusedError, match="inside the namespace"):
        check(project, patch, plan, default_branch="mcpforge/main")


def test_there_is_no_force_push_or_history_rewrite_anywhere() -> None:
    """§6 — never force push, never rewrite history. Asserted structurally."""
    import re

    writer = SRC / "mcpforge" / "github" / "writer.py"
    # Mechanisms, not the word: the module's own error message says "never
    # force-pushes", and flagging that would make this test unmaintainable.
    mechanisms = re.compile(
        r"""["']force["']\s*:"""  # a force flag in a JSON body
        r"|force\s*=\s*True"  # a force keyword argument
        r"|--force"  # a git flag
        r"|filter-branch|rebase"  # history rewriting
        r'|["\'](?:PATCH|PUT|DELETE)["\']'  # HTTP methods that move or remove a ref
    )
    offenders = [
        f"{writer.name}:{lineno}: {code}"
        for lineno, code in code_lines(writer)
        if mechanisms.search(code)
    ]
    assert not offenders, "force/rewrite mechanism in the writer:\n" + "\n".join(offenders)


def test_the_writer_never_deletes_or_updates_a_ref() -> None:
    """create-ref fails if the branch exists and cannot move an existing one."""
    source = (SRC / "mcpforge" / "github" / "writer.py").read_text()
    assert '"POST",\n            f"/repos/{repo}/git/refs"' in source
    assert "DELETE" not in source
    assert "git/refs/heads" not in source.replace("git/ref/heads", "")


# -- access mode and boundary ----------------------------------------------


def test_a_read_only_project_cannot_write(patch: Any, plan: ToolPlan) -> None:
    read_only = bind_repository(
        Project(id="proj_1", owner_uid=OWNER, name="hotel"),
        repository_id="12345",
        full_name=REPO,
        base_branch="main",
    )
    with pytest.raises(AccessModeError):
        check(read_only, patch, plan)


def test_a_demo_project_can_never_reach_the_writer(patch: Any, plan: ToolPlan) -> None:
    """03_SECURITY_ACCESS.md §5 — no bound repository, so no write path, ever."""
    demo = Project(id="proj_1", owner_uid=OWNER, name="demo")
    with pytest.raises(NoRepositoryBoundError):
        check(demo, patch, plan)


def test_writing_to_a_repository_the_project_is_not_bound_to_is_refused(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    with pytest.raises(BoundaryError, match="is bound to"):
        check(project, patch, plan, repository_full_name="someone-else/private")


# -- approvals --------------------------------------------------------------


@pytest.mark.parametrize("missing", ["patch_approval", "pr_approval"])
def test_both_approvals_are_required(
    project: Project, patch: Any, plan: ToolPlan, missing: str
) -> None:
    with pytest.raises(WriteRefusedError, match=r"No .* approval exists"):
        check(project, patch, plan, **{missing: None})


def test_a_pending_approval_does_not_authorise_a_write(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    with pytest.raises(WriteRefusedError, match="does not cover"):
        check(
            project,
            patch,
            plan,
            patch_approval=approval(
                ApprovalGate.PATCH, patch, status=ApprovalStatus.PENDING, decided_at=None
            ),
        )


def test_a_rejected_approval_does_not_authorise_a_write(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    with pytest.raises(WriteRefusedError, match="does not cover"):
        check(
            project,
            patch,
            plan,
            pr_approval=approval(ApprovalGate.PULL_REQUEST, patch, status=ApprovalStatus.REJECTED),
        )


def test_a_patch_changed_after_approval_is_refused(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    """The reason approvals carry a hash: this is the tamper case."""
    tampered = patch.model_copy(
        update={
            "files": [
                *patch.files[:-1],
                patch.files[-1].model_copy(update={"contents": "// something else"}),
            ]
        }
    )
    # Approvals cover the ORIGINAL patch; the tampered one is what would be
    # written. That is the whole point of binding an approval to a hash.
    with pytest.raises(WriteRefusedError, match="patch changed after approval"):
        check(
            project,
            tampered,
            plan,
            patch_approval=approval(ApprovalGate.PATCH, patch),
            pr_approval=approval(ApprovalGate.PULL_REQUEST, patch),
        )


def test_an_approval_from_another_session_is_refused(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    with pytest.raises(WriteRefusedError, match="another run"):
        check(
            project,
            patch,
            plan,
            patch_approval=approval(ApprovalGate.PATCH, patch, session_id="sess_other"),
        )


def test_an_approval_from_another_project_is_refused(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    with pytest.raises(WriteRefusedError, match="another run"):
        check(
            project,
            patch,
            plan,
            pr_approval=approval(ApprovalGate.PULL_REQUEST, patch, project_id="proj_other"),
        )


def test_a_tool_plan_approval_cannot_stand_in_for_a_patch_approval(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    with pytest.raises(WriteRefusedError):
        check(
            project,
            patch,
            plan,
            patch_approval=approval(ApprovalGate.TOOL_PLAN, patch),
        )


# -- policy still applies at write time ------------------------------------


def test_approval_does_not_override_a_policy_violation(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    """A human approving something the policy engine blocks changes nothing.

    03_SECURITY_ACCESS.md §7: authorization is enforced by deterministic code.
    An approval says "the human agreed", not "the rules do not apply".
    """
    ungated = plan.model_copy(
        update={
            "tools": [
                plan.tools[0],
                plan.tools[1].model_copy(update={"approval_required": False}),
            ]
        }
    )
    with pytest.raises(WriteRefusedError, match="Policy blocks this patch"):
        check(project, patch, ungated)


# -- the branch is created, never overwritten -------------------------------


async def test_an_existing_branch_is_never_overwritten(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/git/ref/heads/mcpforge/webmcp-booking"):
            return httpx.Response(200, json={"ref": "refs/heads/mcpforge/webmcp-booking"})
        raise AssertionError(f"unexpected call: {request.method} {request.url.path}")

    writer = BranchAndPullRequestWriter(
        token=token,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="https://api.github.test",
    )
    with pytest.raises(BranchExistsError, match="never force-pushes"):
        await writer.create_pull_request(
            project=project,
            repository_full_name=REPO,
            default_branch="main",
            base_commit="basesha",
            branch=branch_name_for("booking"),
            patch=patch,
            plan=plan,
            patch_approval=approval(ApprovalGate.PATCH, patch),
            pr_approval=approval(ApprovalGate.PULL_REQUEST, patch),
            session_id=SESSION,
            title="t",
            body="b",
        )


async def test_a_pull_request_targets_the_default_branch_from_our_branch(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        path = request.url.path
        body = jsonlib.loads(request.content) if request.content else {}
        calls.append((request.method, path, body))

        if path.endswith("/git/ref/heads/mcpforge/webmcp-booking"):
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blobsha"})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "treesha"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commitsha"})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": body["ref"]})
        if path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.test/pr/7"})
        raise AssertionError(f"unexpected call: {request.method} {path}")

    writer = BranchAndPullRequestWriter(
        token=token,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="https://api.github.test",
    )
    result = await writer.create_pull_request(
        project=project,
        repository_full_name=REPO,
        default_branch="main",
        base_commit="basesha",
        branch=branch_name_for("booking"),
        patch=patch,
        plan=plan,
        patch_approval=approval(ApprovalGate.PATCH, patch),
        pr_approval=approval(ApprovalGate.PULL_REQUEST, patch),
        session_id=SESSION,
        title="Add WebMCP tools",
        body="body",
    )

    assert result.number == 7
    assert result.branch == "mcpforge/webmcp-booking"

    # The ref created is ours, and the commit's parent is the approved base.
    ref_call = next(b for m, p, b in calls if p.endswith("/git/refs"))
    assert ref_call["ref"] == "refs/heads/mcpforge/webmcp-booking"
    commit_call = next(b for m, p, b in calls if p.endswith("/git/commits"))
    assert commit_call["parents"] == ["basesha"]

    # The pull request comes *from* our branch *into* the default branch.
    pr_call = next(b for m, p, b in calls if p.endswith("/pulls"))
    assert pr_call["head"] == "mcpforge/webmcp-booking"
    assert pr_call["base"] == "main"

    # Nothing was ever sent to update or delete a ref.
    assert all(m in ("GET", "POST") for m, _, _ in calls)


async def test_no_write_happens_when_a_precondition_fails(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken
) -> None:
    """The refusal must come before any API call, not after some of them."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    writer = BranchAndPullRequestWriter(
        token=token,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="https://api.github.test",
    )
    with pytest.raises(WriteRefusedError):
        await writer.create_pull_request(
            project=project,
            repository_full_name=REPO,
            default_branch="main",
            base_commit="basesha",
            branch="main",
            patch=patch,
            plan=plan,
            patch_approval=approval(ApprovalGate.PATCH, patch),
            pr_approval=approval(ApprovalGate.PULL_REQUEST, patch),
            session_id=SESSION,
            title="t",
            body="b",
        )
    assert calls == [], f"the writer called GitHub before refusing: {calls}"
