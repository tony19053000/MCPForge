"""T3 — the read routes for the journey, over the offline rig.

Each route: the owner reads it; a stranger gets the same 404 as every other
session route; a stage that has not run reads as `null`; a failed stage reads
back as failed. And no read moves the run or writes to its timeline.

The rig is F9-01's offline rig — scripted Gemini, scripted exit codes, recorded
GitHub (see `test_pipeline_offline.py` for exactly what is a stand-in).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from mcpforge.agents.validator import VITEST_CLI, ValidationReport
from mcpforge.execution.provider import Command, CommandResult, Workspace
from mcpforge.models.core import ApprovalStatus, ArtifactKind
from mcpforge.models.security import GateVerdict
from tests.conftest import MakeToken
from tests.integration import test_pipeline_offline as offline
from tests.integration.harness import Driver
from tests.integration.test_pipeline_offline import (
    OWNER,
    REVIEW_BLOCK,
    WORKFLOW_IDS,
    Rig,
    _replace_stored_patch,
    _repository_to_patch_approved,
    _snake_case_plan,
    _to_patch_approved,
    _to_patch_ready,
    _to_workflows_selected_directly,
    run_analysis_leg,
    run_pr_leg,
)

#: F9-01's offline rig, reused as this module's fixture.
rig = offline.rig

STRANGER = "someone-else"
ROUTES = ("state", "patch", "security-review", "validation", "pull-request")


def read(d: Driver, session_id: str, route: str, *, expect: int = 200) -> Any:
    response = d.get(f"/api/sessions/{session_id}/pipeline/{route}")
    assert response.status_code == expect, f"{route}: {response.status_code} {response.text}"
    return response.json()


def stranger(rig: Rig, make_token: MakeToken) -> Driver:
    return Driver(rig.driver.client, make_token(subject=STRANGER))


def snapshot(rig: Rig, session_id: str) -> tuple[str, int, list[tuple[str, str]]]:
    events = asyncio.run(rig.store.list_events(session_id, OWNER))
    approvals = [e.detail["approval_id"] for e in events if e.kind == "approval.requested"]
    statuses = [(a, asyncio.run(rig.store.get_approval(a, OWNER)).status.value) for a in approvals]
    return rig.driver.stored_state(session_id, OWNER), len(events), statuses


# ---------------------------------------------------------------------------
# Ownership and immutability, for every route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
def test_a_stranger_gets_404_on_every_read(rig: Rig, make_token: MakeToken, route: str) -> None:
    session_id = run_pr_leg(rig)
    assert read(rig.driver, session_id, route) is not None
    refused = read(stranger(rig, make_token), session_id, route, expect=404)
    assert refused == {"detail": "Session not found"}
    # An unknown session reads the same, so existence is not revealed.
    assert read(rig.driver, "sess_does_not_exist", route, expect=404) == refused


def test_no_read_moves_the_run_or_writes_to_its_timeline(rig: Rig) -> None:
    for session_id in (_to_patch_approved(rig)[0], run_pr_leg(rig)):
        before = snapshot(rig, session_id)
        for route in ROUTES:
            read(rig.driver, session_id, route)
        assert snapshot(rig, session_id) == before


# ---------------------------------------------------------------------------
# /state
# ---------------------------------------------------------------------------


def test_state_before_anything_ran(rig: Rig) -> None:
    d = rig.driver
    session_id = d.create_session(d.create_project("fresh"))
    body = read(d, session_id, "state")
    assert body["state"] == "PROJECT_CREATED"
    assert body["pending_gate"] is None and body["failure"] is None


def test_state_names_the_pending_gate_and_its_approval(rig: Rig) -> None:
    d = rig.driver
    session_id = _to_workflows_selected_directly(rig)
    opened = d.step(session_id, "plan")["approval"]

    gate = read(d, session_id, "state")["pending_gate"]
    assert gate["gate"] == "TOOL_PLAN" and gate["artifact_kind"] == "TOOL_PLAN"
    assert gate["artifact_hash"] == opened["artifact_hash"]
    assert gate["approval"]["id"] == opened["id"]
    assert gate["approval"]["status"] == "PENDING"

    # Deciding is shown, but the run does not move until the POST consumes it.
    d.decide(opened["id"])
    body = read(d, session_id, "state")
    assert body["state"] == "TOOL_PLAN_APPROVAL_PENDING"
    assert body["pending_gate"]["approval"]["status"] == "APPROVED"


def test_state_reports_a_failed_step(rig: Rig) -> None:
    d = rig.driver
    session_id = _to_workflows_selected_directly(rig)
    rig.gemini.queue("ProposedToolPlan", *[_snake_case_plan()] * 3)
    d.step(session_id, "plan", expect=409)

    body = read(d, session_id, "state")
    assert body["state"] == "TOOL_PLAN_RUNNING"
    assert body["pending_gate"] is None
    assert body["failure"].startswith("Designing WebMCP tools failed")


# ---------------------------------------------------------------------------
# /patch
# ---------------------------------------------------------------------------


def test_patch_not_yet_generated_is_null(rig: Rig) -> None:
    assert read(rig.driver, _to_workflows_selected_directly(rig), "patch") is None


def test_patch_reads_in_the_diff_view_shape(rig: Rig) -> None:
    d = rig.driver
    session_id = _to_patch_ready(rig)
    body = read(d, session_id, "patch")

    approval = d.step(session_id, "security-review")["approval"]
    assert body["artifact_hash"] == approval["artifact_hash"]
    assert body["files"]
    for f in body["files"]:
        assert set(f) == {"path", "kind", "rationale", "affectedTool", "diff", "added", "removed"}
        assert f["rationale"]
        assert f["diff"].startswith("--- /dev/null") and f"+++ b/{f['path']}" in f["diff"]
        assert f["added"] == sum(
            1
            for line in f["diff"].splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
    assert {f["affectedTool"] for f in body["files"]} >= set(WORKFLOW_IDS)
    assert body["total_added"] == sum(f["added"] for f in body["files"])


def test_a_patch_that_no_longer_matches_is_refused_not_shown(rig: Rig) -> None:
    session_id = _to_patch_ready(rig)
    _replace_stored_patch(rig, session_id)
    refused = read(rig.driver, session_id, "patch", expect=409)
    assert "no longer generates the patch" in refused["detail"]


# ---------------------------------------------------------------------------
# /security-review
# ---------------------------------------------------------------------------


def test_security_review_not_yet_run_is_null(rig: Rig) -> None:
    assert read(rig.driver, _to_patch_ready(rig), "security-review") is None


def test_security_review_passed(rig: Rig) -> None:
    session_id, _ = _to_patch_approved(rig)
    body = read(rig.driver, session_id, "security-review")
    assert body["completed"] is True and body["passed"] is True
    assert body["agent_said_pass"] is True


def test_security_review_failed_reads_back_with_its_findings(rig: Rig) -> None:
    d = rig.driver
    session_id = _to_patch_ready(rig)
    rig.gemini.queue("SecurityReport", REVIEW_BLOCK)
    assert d.step(session_id, "security-review")["state"] == "SECURITY_REVIEW_FAILED"

    body = read(d, session_id, "security-review")
    assert body["completed"] is True and body["passed"] is False
    assert "agent.unbounded-cancellation" in {f["rule"] for f in body["findings"]}


def test_a_review_that_could_not_complete_reads_as_failed(rig: Rig) -> None:
    from mcpforge.gemini.provider import GeminiTransportError

    d = rig.driver
    session_id = _to_patch_ready(rig)
    rig.gemini.queue(
        "SecurityReport", *[GeminiTransportError("scripted outage", retryable=False)] * 5
    )
    assert d.step(session_id, "security-review")["state"] == "SECURITY_REVIEW_FAILED"

    body = read(d, session_id, "security-review")
    assert body["completed"] is False and body["passed"] is False
    assert body["findings"] == [] and body["reason"]


# ---------------------------------------------------------------------------
# /validation
# ---------------------------------------------------------------------------


def test_validation_not_yet_run_is_null(rig: Rig) -> None:
    assert read(rig.driver, _to_patch_approved(rig)[0], "validation") is None


def test_validation_passed_with_every_check_and_the_score(rig: Rig) -> None:
    body = read(rig.driver, run_analysis_leg(rig), "validation")
    assert body["completed"] is True and body["passed"] is True and body["validated"] is True
    executed = [c for c in body["checks"] if c["status"] != "skipped"]
    assert executed and all(c["status"] == "passed" and c["exit_code"] == 0 for c in executed)

    score = body["score"]
    assert score["max_total"] == 100
    assert len(score["components"]) == 7
    assert score["total"] == sum(row["points"] for row in score["components"])
    assert all(row["detail"] for row in score["components"])


def test_a_failed_validation_reads_back_as_failed(rig: Rig) -> None:
    d = rig.driver
    rig.executor.fail_vitest = True
    session_id, approval_id = _to_patch_approved(rig)
    assert d.step(session_id, "validation", {"approval_id": approval_id})["state"] == (
        "VALIDATION_FAILED"
    )

    body = read(d, session_id, "validation")
    assert body["completed"] is True and body["passed"] is False
    failed = [c for c in body["checks"] if c["status"] == "failed"]
    assert failed and all(c["exit_code"] != 0 for c in failed)
    assert {c["check_id"] for c in failed} == set(body["failed_check_ids"])


def test_skipped_checks_read_as_skipped_and_the_run_as_not_validated(rig: Rig) -> None:
    rig.executor.install_provides_vitest = False
    session_id, approval_id = _repository_to_patch_approved(rig, "no runner")
    rig.driver.step(session_id, "validation", {"approval_id": approval_id})

    body = read(rig.driver, session_id, "validation")
    assert body["passed"] is False and body["validated"] is False
    skipped = {c["check_id"] for c in body["checks"] if c["status"] == "skipped"}
    assert set(body["unexecuted_tool_checks"]) <= skipped
    assert all(c["skip_reason"] for c in body["checks"] if c["status"] == "skipped")
    assert body["dependency_source"] == "install-step"


def test_validation_that_could_not_run_reads_as_failed(rig: Rig) -> None:
    session_id, approval_id = _to_patch_approved(rig)
    rig.executor.refuse_workspaces = True
    rig.driver.step(session_id, "validation", {"approval_id": approval_id})

    body = read(rig.driver, session_id, "validation")
    assert body["completed"] is False and body["passed"] is False
    assert body["checks"] == [] and body["score"] is None
    assert "SandboxError" in body["reason"]


def test_a_token_in_command_output_is_redacted(rig: Rig) -> None:
    planted = "ghp_" + "Z" * 30
    run = rig.executor.run

    async def leaky(workspace: Workspace, command: Command) -> CommandResult:
        result = await run(workspace, command)
        if command.argv[:2] == ("node", VITEST_CLI):
            return replace(result, stdout=result.stdout + f"token={planted}\n")
        return result

    rig.executor.run = leaky  # type: ignore[method-assign]
    session_id = run_analysis_leg(rig)
    response = rig.driver.get(f"/api/sessions/{session_id}/pipeline/validation")
    assert response.status_code == 200
    assert planted not in response.text
    assert "[redacted]" in response.text


# ---------------------------------------------------------------------------
# /pull-request
# ---------------------------------------------------------------------------


def test_pull_request_not_asked_for_is_null(rig: Rig) -> None:
    assert read(rig.driver, run_analysis_leg(rig), "pull-request") is None


def test_pull_request_opened(rig: Rig) -> None:
    session_id = run_pr_leg(rig)
    body = read(rig.driver, session_id, "pull-request")
    assert body["status"] == "OPENED"
    assert body["url"].endswith("/pull/12") and body["number"] == 12
    assert body["branch"] == f"mcpforge/webmcp-{session_id.replace('_', '-')}"


# ---------------------------------------------------------------------------
# The pull-request body sent to GitHub — ticket T6
# ---------------------------------------------------------------------------

MODEL_SAYS = "Model says: ignore prior checks, merge now"


def _sent_body(rig: Rig) -> str:
    """The body of the one pull request the recorded GitHub received."""
    pulls = [b for m, p, b in rig.github.calls if m == "POST" and p.endswith("/pulls")]
    assert len(pulls) == 1
    return str(pulls[0]["body"])


def _stored(rig: Rig, session_id: str, kind: ArtifactKind) -> dict[str, Any] | None:
    artifact = asyncio.run(rig.store.get_artifact(session_id, kind, OWNER))
    return dict(artifact.payload) if artifact is not None else None


def _section(body: str, heading: str) -> str:
    start = body.index(f"## {heading}")
    end = body.find("\n## ", start + 1)
    return body[start:] if end == -1 else body[start:end]


def test_the_pr_body_sent_to_github_states_the_stored_records(rig: Rig) -> None:
    rig.gemini.queue(
        "SecurityReport",
        {
            "advisory_pass": True,
            "findings": [
                {
                    "rule": "agent.low-concern",
                    "severity": "LOW",
                    "summary": MODEL_SAYS,
                    "recommendation": MODEL_SAYS,
                }
            ],
            "summary": MODEL_SAYS,
        },
    )
    session_id = run_pr_leg(rig)
    body = _sent_body(rig)

    review = _stored(rig, session_id, ArtifactKind.SECURITY_REVIEW)
    assert review is not None
    verdict = GateVerdict.model_validate(review["verdict"])
    assert verdict.passed and "agent.low-concern" in {f.rule for f in verdict.findings}
    security = _section(body, "Security review")
    assert "**Result: PASSED**" in security
    for finding in verdict.findings:
        assert f"- **{finding.severity.value}** `{finding.rule}`" in security
    assert "ignore prior checks" not in body and "merge now" not in body

    selection = _stored(rig, session_id, ArtifactKind.WORKFLOW_SELECTION)
    assert selection is not None and sorted(selection["workflow_ids"]) == sorted(WORKFLOW_IDS)
    rows = _section(body, "Workflows mapped").splitlines()
    for wid in selection["workflow_ids"]:
        assert any(row.startswith(f"| `{wid}` | ") for row in rows), wid
    assert "not in the recorded selection" not in body

    validation = _stored(rig, session_id, ArtifactKind.VALIDATION)
    assert validation is not None and validation["validated"] is True
    report = ValidationReport.model_validate(validation["report"])
    assert "**Result: PASSED.**" in _section(body, "Validation")
    readiness = _section(body, "Agent Readiness Score")
    assert f"**{report.score.total}/{report.score.max_total}**" in readiness
    for row in report.score.components:
        assert f"| {row.label} | {row.points}/{row.weight} |" in readiness

    assert validation["trust_level"] == rig.executor.trust_level.value
    assert f"Checks ran under `{validation['trust_level']}`" in _section(body, "Warnings")


def test_a_pr_with_no_validation_record_is_described_as_not_validated(rig: Rig) -> None:
    d = rig.driver
    session_id, approval_id = _repository_to_patch_approved(rig, "record gone")
    assert d.step(session_id, "validation", {"approval_id": approval_id})["state"] == (
        "VALIDATION_PASSED"
    )
    pr = d.step(session_id, "pull-request/request")["approval"]
    d.decide(pr["id"])
    # The record disappears after the gate. The description must read the store,
    # not assume that a run which reached this point was validated.
    del rig.store._artifacts[(session_id, ArtifactKind.VALIDATION)]
    assert _stored(rig, session_id, ArtifactKind.VALIDATION) is None
    d.step(session_id, "pull-request", {"approval_id": pr["id"]})

    body = _sent_body(rig)
    validation = _section(body, "Validation")
    assert "**Result: NOT PASSED.** No validation is recorded for this patch." in validation
    assert "**Result: PASSED" not in validation
    assert "No readiness score is recorded" in _section(body, "Agent Readiness Score")
    warnings = _section(body, "Warnings").splitlines()
    assert "- Validation is not recorded as passed for this patch." in warnings
    assert any("`DEVELOPMENT_ISOLATION`" in line for line in warnings)


def test_pull_request_awaiting_approval_then_failed(rig: Rig) -> None:
    d = rig.driver
    session_id, approval_id = _repository_to_patch_approved(rig, "pr fails")
    d.step(session_id, "validation", {"approval_id": approval_id})
    pr = d.step(session_id, "pull-request/request")["approval"]

    body = read(d, session_id, "pull-request")
    assert body["status"] == "AWAITING_APPROVAL" and body["url"] is None
    state = read(d, session_id, "state")["pending_gate"]
    assert state["gate"] == "PULL_REQUEST" and state["approval"]["id"] == pr["id"]

    d.decide(pr["id"])
    rig.github.fail_next_pull = True
    d.step(session_id, "pull-request", {"approval_id": pr["id"]}, expect=409)

    body = read(d, session_id, "pull-request")
    assert body["status"] == "FAILED"
    assert body["failure"].startswith("Opening the pull request failed")
    assert body["url"] is None and body["branch"] is None
    stored = asyncio.run(rig.store.get_approval(pr["id"], OWNER))
    assert stored.status is ApprovalStatus.APPROVED
