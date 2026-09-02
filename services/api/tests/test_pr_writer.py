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
from mcpforge.github.branches import BRANCH_PREFIX, branch_name_for
from mcpforge.github.client import InstallationToken
from mcpforge.github.writer import (
    BranchAndPullRequestWriter,
    BranchExistsError,
    WriteRefusedError,
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
from mcpforge.orchestration.recovery import WriteStage
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
        "base_commit": "basesha",
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
    with pytest.raises(WriteRefusedError, match="writes only to branches of the form"):
        check(project, patch, plan, branch=branch)


@pytest.mark.parametrize("name", ["main", "master", "develop", "production"])
def test_a_protected_name_under_our_prefix_is_still_refused(
    project: Project, patch: Any, plan: ToolPlan, name: str
) -> None:
    # The shape check refuses these first — `mcpforge/main` is not
    # `mcpforge/webmcp-<slug>` — and the protected-name check stands behind it.
    with pytest.raises(WriteRefusedError):
        check(project, patch, plan, branch=f"{BRANCH_PREFIX}{name}")
    with pytest.raises(WriteRefusedError, match="protected name"):
        check(
            project,
            patch,
            plan,
            branch=f"{BRANCH_PREFIX}webmcp-{name}",
            default_branch=f"{BRANCH_PREFIX}webmcp-{name}",
        )


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


@pytest.mark.parametrize(
    "branch",
    [
        "mcpforge/../../../other",
        "mcpforge/webmcp-a/../../../other",
        "mcpforge/webmcp-a%2f..%2f..",
        "mcpforge/webmcp- space",
        "mcpforge/webmcp-a\nb",
        "mcpforge/webmcp-a\tb",
        "mcpforge/webmcp-UPPER",
        "mcpforge/webmcp-",
        "mcpforge/webmcp-" + "a" * 100,
        "MCPFORGE/webmcp-a",
    ],
)
def test_a_branch_name_that_is_not_exactly_our_shape_is_refused(
    project: Project, patch: Any, plan: ToolPlan, branch: str
) -> None:
    """A prefix check is not enough.

    httpx collapses dot segments, so `mcpforge/../../../other` would pass a
    prefix test and silently retarget the existence probe at an unrelated
    endpoint — which 404s, and would be read as "the branch does not exist".
    """
    with pytest.raises(WriteRefusedError, match="writes only to branches of the form"):
        check(project, patch, plan, branch=branch)


def test_httpx_really_does_collapse_dot_segments() -> None:
    """The reason the shape check exists, asserted rather than assumed."""
    url = httpx.URL("https://api.github.test/repos/o/r/git/ref/heads/mcpforge/../../../other")
    assert "mcpforge" not in str(url)


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
        r'|["\'](?:PATCH|PUT)["\']'  # HTTP methods that would MOVE a ref
    )
    offenders = [
        f"{writer.name}:{lineno}: {code}"
        for lineno, code in code_lines(writer)
        if mechanisms.search(code)
    ]
    assert not offenders, "force/rewrite mechanism in the writer:\n" + "\n".join(offenders)
    # DELETE is covered separately: it exists once, for cleanup, behind
    # may_delete_branch. See test_the_writer_creates_refs_and_never_updates_one.


def test_the_writer_creates_refs_and_never_updates_one() -> None:
    """create-ref fails if the branch exists and cannot move an existing one.

    A DELETE does exist, for cleanup — but it is reachable only through
    `_try_cleanup`, which requires `may_delete_branch`.
    """
    source = (SRC / "mcpforge" / "github" / "writer.py").read_text()
    assert '"POST",\n                f"/repos/{repo}/git/refs"' in source

    deletes = [
        (lineno, code)
        for lineno, code in code_lines(SRC / "mcpforge" / "github" / "writer.py")
        if '"DELETE"' in code
    ]
    assert len(deletes) == 1, f"more than one delete path: {deletes}"

    cleanup = source[source.index("async def _try_cleanup") :]
    assert "may_delete_branch(" in cleanup
    assert cleanup.index("may_delete_branch(") < cleanup.index('"DELETE"')


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
        if path.endswith("/git/commits/basesha"):
            return httpx.Response(200, json={"tree": {"sha": "basetreesha"}})
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
    )

    assert result.succeeded is True
    assert result.branch == "mcpforge/webmcp-booking"
    assert result.pull_request is not None
    assert result.pull_request.number == 7  # type: ignore[attr-defined]

    # The ref created is ours, and the commit's parent is the approved base.
    ref_call = next(b for m, p, b in calls if p.endswith("/git/refs"))
    assert ref_call["ref"] == "refs/heads/mcpforge/webmcp-booking"
    commit_call = next(b for m, p, b in calls if p.endswith("/git/commits"))
    assert commit_call["parents"] == ["basesha"]
    # The tree is built on the base commit's TREE, not on the commit sha.
    tree_call = next(b for m, p, b in calls if p.endswith("/git/trees"))
    assert tree_call["base_tree"] == "basetreesha"

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
        )
    assert calls == [], f"the writer called GitHub before refusing: {calls}"


# -- injected failure at each step — F6-04 ---------------------------------
#
# The earlier recovery tests handed the stage in, so nothing verified that a
# real failure at the commit step produces COMMIT_CREATED. These drive the
# writer with a transport that fails at one step at a time.


def _transport(fail_at: str | None) -> tuple[httpx.MockTransport, list[tuple[str, str]]]:
    """A GitHub that works until `fail_at`, then returns 500."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))

        def maybe(step: str, response: httpx.Response) -> httpx.Response:
            failing = fail_at == step or (fail_at == "cleanup" and step == "pull")
            return httpx.Response(500, json={"message": "boom"}) if failing else response

        if "/git/ref/heads/" in path:
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/git/commits/basesha"):
            return maybe("base", httpx.Response(200, json={"tree": {"sha": "treesha"}}))
        if path.endswith("/git/blobs"):
            return maybe("blob", httpx.Response(201, json={"sha": "blobsha"}))
        if path.endswith("/git/trees"):
            return maybe("tree", httpx.Response(201, json={"sha": "newtree"}))
        if path.endswith("/git/commits"):
            return maybe("commit", httpx.Response(201, json={"sha": "newcommit"}))
        if path.endswith("/git/refs"):
            return maybe("ref", httpx.Response(201, json={"ref": "refs/heads/x"}))
        if path.endswith("/pulls"):
            return maybe("pull", httpx.Response(201, json={"number": 9, "html_url": "u"}))
        if request.method == "DELETE":
            # `cleanup` means the pull request failed AND the delete failed.
            return httpx.Response(500) if fail_at == "cleanup" else httpx.Response(204)
        raise AssertionError(f"unexpected: {request.method} {path}")

    return httpx.MockTransport(handler), calls


async def _run(
    project: Project,
    patch: Any,
    plan: ToolPlan,
    token: InstallationToken,
    fail_at: str | None,
) -> tuple[Any, list[tuple[str, str]]]:
    transport, calls = _transport(fail_at)
    writer = BranchAndPullRequestWriter(
        token=token,
        http=httpx.AsyncClient(transport=transport),
        base_url="https://api.github.test",
    )
    outcome = await writer.create_pull_request(
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
    )
    return outcome, calls


@pytest.mark.parametrize("fail_at", ["base", "blob", "tree", "commit"])
async def test_a_failure_before_the_branch_exists_leaves_nothing_behind(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken, fail_at: str
) -> None:
    outcome, calls = await _run(project, patch, plan, token, fail_at)

    assert outcome.succeeded is False
    assert outcome.stage is WriteStage.NOTHING_DONE
    assert outcome.cleanup_performed is False
    assert "500" in (outcome.error or "")
    assert "Nothing was written" in outcome.explain()
    # No ref was created, so there is nothing to delete.
    assert not any(m == "DELETE" for m, _ in calls)


async def test_a_failure_creating_the_ref_reports_an_orphaned_commit(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken
) -> None:
    """The commit exists but no branch points at it, so nothing the developer
    sees has changed."""
    outcome, calls = await _run(project, patch, plan, token, "ref")

    assert outcome.stage is WriteStage.COMMIT_CREATED
    assert "no branch points at it" in outcome.explain()
    assert not any(m == "DELETE" for m, _ in calls)


async def test_a_failure_opening_the_pull_request_cleans_up_our_branch(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken
) -> None:
    """This is the case recovery.py exists for: a branch we created, and no
    pull request pointing at it."""
    outcome, calls = await _run(project, patch, plan, token, "pull")

    assert outcome.stage is WriteStage.BRANCH_CREATED
    assert outcome.cleanup_performed is True
    assert "was removed" in outcome.explain()

    deletes = [p for m, p in calls if m == "DELETE"]
    assert deletes == ["/repos/tony19053000/mcpforge-test/git/refs/heads/mcpforge/webmcp-booking"]


async def test_a_failed_cleanup_is_reported_honestly(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken
) -> None:
    """If we could not remove the branch, say so and name it, rather than
    claiming a tidy state."""
    outcome, _ = await _run(project, patch, plan, token, "cleanup")

    assert outcome.stage is WriteStage.BRANCH_CREATED
    assert outcome.cleanup_performed is False
    assert "still there" in outcome.explain()
    assert "mcpforge/webmcp-booking" in outcome.explain()


async def test_the_happy_path_reports_success_and_deletes_nothing(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken
) -> None:
    outcome, calls = await _run(project, patch, plan, token, None)

    assert outcome.succeeded is True
    assert outcome.stage is WriteStage.PULL_REQUEST_OPENED
    assert outcome.pull_request is not None
    assert not any(m == "DELETE" for m, _ in calls)


# -- the pull request describes what it contains — F6-02 -------------------


async def test_the_pull_request_body_is_built_from_the_plan_and_patch(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken
) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        path = request.url.path
        if "/git/ref/heads/" in path:
            return httpx.Response(404, json={})
        if path.endswith("/git/commits/basesha"):
            return httpx.Response(200, json={"tree": {"sha": "t"}})
        if path.endswith("/pulls"):
            bodies.append(jsonlib.loads(request.content))
            return httpx.Response(201, json={"number": 1, "html_url": "u"})
        return httpx.Response(201, json={"sha": "s", "ref": "r"})

    writer = BranchAndPullRequestWriter(
        token=token,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="https://api.github.test",
    )
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
    )

    body = bodies[0]["body"]
    # Every tool, its risk, and the function it calls.
    assert "`search_rooms`" in body and "`cancel_reservation`" in body
    assert "`searchRooms()`" in body and "`cancelReservation()`" in body
    assert "DESTRUCTIVE" in body
    # The files, with their reasons.
    assert "src/webmcp/tools/cancelReservation.ts" in body
    # What stops for the developer.
    assert "What stops for you" in body
    assert "an AI agent cannot complete them without a person deciding" in body
    # No claim about validation that did not run.
    assert "## Validation" not in body


async def test_a_credential_in_the_pull_request_body_blocks_the_write(
    project: Project, patch: Any, plan: ToolPlan, token: InstallationToken
) -> None:
    """§4.4 — outbound content is scanned again before the pull request. The
    body is outbound content, and it is built from model-authored descriptions.
    """
    leaked = "AKIA" + "IOSFODNN7EXAMPLE"
    tainted = plan.model_copy(
        update={
            "tools": [
                plan.tools[0].model_copy(update={"description": f"Use {leaked} to search"}),
                plan.tools[1],
            ]
        }
    )
    transport, calls = _transport(None)
    writer = BranchAndPullRequestWriter(
        token=token,
        http=httpx.AsyncClient(transport=transport),
        base_url="https://api.github.test",
    )
    with pytest.raises(WriteRefusedError, match="credential-shaped"):
        await writer.create_pull_request(
            project=project,
            repository_full_name=REPO,
            default_branch="main",
            base_commit="basesha",
            branch=branch_name_for("booking"),
            patch=patch,
            plan=tainted,
            patch_approval=approval(ApprovalGate.PATCH, patch),
            pr_approval=approval(ApprovalGate.PULL_REQUEST, patch),
            session_id=SESSION,
        )
    assert calls == [], "the writer contacted GitHub before scanning the body"


# -- the base commit is bound to the approval — F6-02 ----------------------


def test_writing_onto_a_different_base_than_the_one_reviewed_is_refused(
    project: Project, patch: Any, plan: ToolPlan
) -> None:
    """The same files on a different base is a different change."""
    with pytest.raises(WriteRefusedError, match="base moved after approval"):
        check(project, patch, plan, base_commit="a-different-sha")


def test_a_patch_with_no_recorded_base_is_refused(project: Project, plan: ToolPlan) -> None:
    baseless = generate_patch(WebMCPToolset(tools=[read_tool(), destructive_tool()]))
    assert baseless.base_commit is None
    with pytest.raises(WriteRefusedError, match="records no base commit"):
        check(
            project,
            baseless,
            plan,
            patch_approval=approval(ApprovalGate.PATCH, baseless),
            pr_approval=approval(ApprovalGate.PULL_REQUEST, baseless),
        )
