import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JourneyPanel, type JourneyApi } from "@/components/pipeline/journey-panel";
import { safeHttpsUrl, stageStatuses } from "@/components/pipeline/journey";
import { ApiError } from "@/lib/api/client";
import type {
  AgentArtifactDto,
  ApprovalDto,
  ApprovalGate,
  PatchDto,
  PendingGateDto,
  PipelineStepDto,
  PullRequestDto,
  RunState,
  RunStateDto,
  SecurityReviewDto,
  ValidationDto,
} from "@/lib/api/types";

/**
 * The journey from generation to the pull request — ticket T5b. The API client
 * is replaced at its boundary (the injected `api`); every payload has the shape
 * the backend's read routes return (`services/api/src/mcpforge/api/pipeline.py`).
 */

const PLAN_HASH = "plan_hash_0001abcd";
const PATCH_HASH = "patch_hash_0001abcdef";

const ANALYSIS: AgentArtifactDto = {
  available: true,
  kind: "ANALYSIS",
  artifact_hash: "an_hash_0001",
  payload: { analysis: { workflows: [] } },
};

const PLAN: AgentArtifactDto = {
  available: true,
  kind: "TOOL_PLAN",
  artifact_hash: PLAN_HASH,
  payload: {
    plan: {
      tools: [
        {
          name: "search_hotels",
          title: "Search hotels",
          description: "Search hotels by city.",
          workflow_id: "search_hotels",
          maps_to_function: "searchHotels",
          parameters: [],
          output_description: "A list of hotels",
          risk: "READ",
          evidence: [{ path: "src/lib/hotels.ts", symbol: "searchHotels", line: 3 }],
          approval_required: false,
        },
      ],
      notes: [],
    },
    types_not_checked: {},
  },
};

const PATCH: PatchDto = {
  artifact_hash: PATCH_HASH,
  summary: "1 file for 1 tool",
  base_commit: "abc123def4567890",
  total_added: 2,
  total_removed: 0,
  files: [
    {
      path: "src/webmcp/tools.ts",
      kind: "add",
      rationale: "Registers the WebMCP tools.",
      affectedTool: "search_hotels",
      diff: "--- /dev/null\n+++ b/src/webmcp/tools.ts\n@@ -0,0 +1,2 @@\n+export const a = 1;\n+export const b = 2;",
      added: 2,
      removed: 0,
    },
  ],
};

const REVIEW_PASS: SecurityReviewDto = {
  completed: true,
  passed: true,
  reason: "No finding at or above HIGH.",
  agent_said_pass: true,
  overridden: false,
  findings: [
    {
      rule: "webmcp.gated_tool",
      severity: "LOW",
      summary: "The destructive tool asks a human first.",
      recommendation: "None.",
      evidence: { path: "src/webmcp/tools.ts", symbol: null, line: 4 },
      deterministic: true,
    },
  ],
};

const REVIEW_FAIL: SecurityReviewDto = {
  completed: true,
  passed: false,
  reason: "1 HIGH finding blocks the gate.",
  agent_said_pass: true,
  overridden: true,
  findings: [
    {
      rule: "xss.unescaped_html",
      severity: "HIGH",
      summary: "Tool output is written as HTML.",
      recommendation: "Escape it.",
      evidence: { path: "src/webmcp/tools.ts", symbol: "render", line: 9 },
      deterministic: false,
    },
  ],
};

const REVIEW_INCOMPLETE: SecurityReviewDto = {
  completed: false,
  passed: false,
  reason: "The reviewer returned no report.",
  agent_said_pass: null,
  overridden: false,
  findings: [],
};

const SCORE: ValidationDto["score"] = {
  total: 60,
  max_total: 100,
  components: [
    {
      component: "TOOL_DISCOVERY",
      label: "Tool discovery",
      weight: 20,
      points: 20,
      checks_passed: 1,
      checks_executed: 1,
      detail: "1 tool registered and discovered",
    },
    {
      component: "REGRESSION",
      label: "Regression",
      weight: 5,
      points: 0,
      checks_passed: 0,
      checks_executed: 0,
      detail: "no regression suite was executed",
    },
  ],
};

const VALIDATION_PASS: ValidationDto = {
  completed: true,
  passed: true,
  validated: true,
  summary: "2/2 executed checks passed",
  reason: null,
  failed_check_ids: [],
  unexecuted_tool_checks: [],
  checks: [
    {
      check_id: "typecheck",
      component: "SCHEMA_VALIDATION",
      description: "tsc --noEmit",
      status: "passed",
      exit_code: 0,
      timed_out: false,
      duration_seconds: 4.2,
      stdout_excerpt: "",
      stderr_excerpt: "",
      skip_reason: null,
    },
    {
      check_id: "regression",
      component: "REGRESSION",
      description: "npm test",
      status: "skipped",
      exit_code: null,
      timed_out: null,
      duration_seconds: null,
      stdout_excerpt: "",
      stderr_excerpt: "",
      skip_reason: "The repository defines no test script.",
    },
  ],
  score: SCORE,
  dependency_source: "demo-fixture",
  dependency_detail: "The bundled demo fixture's dependency tree.",
};

const VALIDATION_FAIL: ValidationDto = {
  ...VALIDATION_PASS,
  passed: false,
  summary: "1/2 executed checks passed",
  failed_check_ids: ["build"],
  checks: [
    VALIDATION_PASS.checks[0]!,
    {
      check_id: "build",
      component: "EXECUTION",
      description: "next build",
      status: "failed",
      exit_code: 2,
      timed_out: false,
      duration_seconds: 12,
      stdout_excerpt: "",
      stderr_excerpt: "Type error: TS2322",
      skip_reason: null,
    },
  ],
};

const VALIDATION_COULD_NOT_RUN: ValidationDto = {
  completed: false,
  passed: false,
  validated: false,
  summary: null,
  reason: "npm ci failed: lockfile missing",
  failed_check_ids: [],
  unexecuted_tool_checks: [],
  checks: [],
  score: null,
  dependency_source: "install-step",
  dependency_detail: "npm ci failed",
};

const PR_OPENED: PullRequestDto = {
  status: "OPENED",
  url: "https://github.com/tony19053000/mcpforge-test/pull/7",
  number: 7,
  branch: "mcpforge/sess_1",
  failure: null,
};

const PR_FAILED: PullRequestDto = {
  status: "FAILED",
  url: null,
  number: null,
  branch: null,
  failure: "Opening the pull request: the base branch moved",
};

function approval(gate: ApprovalGate, over: Partial<ApprovalDto> = {}): ApprovalDto {
  const byGate: Record<string, { id: string; hash: string; summary: string }> = {
    TOOL_PLAN: { id: "appr_plan", hash: PLAN_HASH, summary: "Approve the WebMCP tool plan" },
    PATCH: { id: "appr_patch", hash: PATCH_HASH, summary: "Approve the generated patch: 1 file(s)" },
    PULL_REQUEST: {
      id: "appr_pr",
      hash: PATCH_HASH,
      summary: "Open a pull request on tony19053000/mcpforge-test",
    },
  };
  const g = byGate[gate]!;
  return {
    id: g.id,
    gate,
    artifact_hash: g.hash,
    summary: g.summary,
    status: "PENDING",
    requested_at: "2026-09-12T10:00:00Z",
    decided_at: null,
    actor_uid: null,
    ...over,
  };
}

const GATE_OF: Partial<Record<RunState, PendingGateDto["gate"]>> = {
  TOOL_PLAN_APPROVAL_PENDING: "TOOL_PLAN",
  PATCH_APPROVAL_PENDING: "PATCH",
  PR_APPROVAL_PENDING: "PULL_REQUEST",
};

function runState(
  state: RunState,
  gateApproval: ApprovalDto | null = null,
  failure: string | null = null,
): RunStateDto {
  const gate = GATE_OF[state];
  return {
    session_id: "sess_1",
    project_id: "proj_1",
    state,
    updated_at: "2026-09-12T10:00:00Z",
    pending_gate: gate
      ? {
          gate,
          artifact_kind: gate === "TOOL_PLAN" ? "TOOL_PLAN" : "PATCH",
          artifact_hash: gate === "TOOL_PLAN" ? PLAN_HASH : PATCH_HASH,
          approval: gateApproval,
        }
      : null,
    failure,
  };
}

function step(state: RunState): PipelineStepDto {
  return { session_id: "sess_1", state, detail: "ok", approval: null, pull_request_url: null };
}

function makeApi() {
  return {
    pipelineState: vi.fn<JourneyApi["pipelineState"]>(),
    pipelineConnect: vi.fn<JourneyApi["pipelineConnect"]>(),
    pipelineAnalyze: vi.fn<JourneyApi["pipelineAnalyze"]>(),
    pipelineSelectWorkflows: vi.fn<JourneyApi["pipelineSelectWorkflows"]>(),
    pipelinePlan: vi.fn<JourneyApi["pipelinePlan"]>(),
    pipelineReject: vi.fn<JourneyApi["pipelineReject"]>(),
    pipelineGenerate: vi.fn<JourneyApi["pipelineGenerate"]>(),
    pipelineSecurityReview: vi.fn<JourneyApi["pipelineSecurityReview"]>(),
    pipelineValidate: vi.fn<JourneyApi["pipelineValidate"]>(),
    pipelineRequestPullRequest: vi.fn<JourneyApi["pipelineRequestPullRequest"]>(),
    pipelineCreatePullRequest: vi.fn<JourneyApi["pipelineCreatePullRequest"]>(),
    pipelinePatch: vi.fn<JourneyApi["pipelinePatch"]>(async () => PATCH),
    pipelineSecurityReviewResult: vi.fn<JourneyApi["pipelineSecurityReviewResult"]>(
      async () => REVIEW_PASS,
    ),
    pipelineValidationResult: vi.fn<JourneyApi["pipelineValidationResult"]>(
      async () => VALIDATION_PASS,
    ),
    pipelinePullRequest: vi.fn<JourneyApi["pipelinePullRequest"]>(async () => null),
    agentWorkflows: vi.fn<JourneyApi["agentWorkflows"]>(async () => ANALYSIS),
    agentPlan: vi.fn<JourneyApi["agentPlan"]>(async () => PLAN),
    decideApproval: vi.fn<JourneyApi["decideApproval"]>(),
  };
}

let api: ReturnType<typeof makeApi>;

beforeEach(() => {
  api = makeApi();
});

function renderPanel() {
  return render(<JourneyPanel api={api as unknown as JourneyApi} sessionId="sess_1" />);
}

function stageBadge(label: string) {
  const list = screen.getByRole("list", { name: "Journey stages" });
  const item = within(list).getByText(label).closest("li");
  if (!item) throw new Error(`no stage ${label}`);
  return item;
}

function section(name: string) {
  return screen.getByRole("heading", { name }).closest("section") as HTMLElement;
}

/** The verdict badge's row: the heading and the badge beside it, not the check rows. */
function verdict(name: string) {
  return screen.getByRole("heading", { name }).parentElement as HTMLElement;
}

/** Waits for the first render, then for every read that render triggers. */
async function settled(heading: string) {
  expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
}

const WRITE_ACTIONS = [
  "pipelineGenerate",
  "pipelineSecurityReview",
  "pipelineValidate",
  "pipelineRequestPullRequest",
  "pipelineCreatePullRequest",
  "pipelineReject",
  "decideApproval",
] as const;

function expectNoWrites() {
  for (const name of WRITE_ACTIONS) expect(api[name], name).not.toHaveBeenCalled();
}

// ---------------------------------------------------------------------------

describe("stage status from run state", () => {
  const status = (state: RunState, id: string, failure: string | null = null) =>
    stageStatuses(state, failure).find((s) => s.stage.id === id)?.status;

  it("a failed security review is failed, never done", () => {
    expect(status("SECURITY_REVIEW_FAILED", "review")).toBe("failed");
    expect(status("SECURITY_REVIEW_FAILED", "generate")).toBe("done");
    expect(status("SECURITY_REVIEW_FAILED", "approve_patch")).toBe("not_yet");
  });

  it("a failed validation is failed, never done", () => {
    expect(status("VALIDATION_FAILED", "validate")).toBe("failed");
    expect(status("VALIDATION_FAILED", "approve_pr")).toBe("not_yet");
  });

  it("a failed pull-request write is failed, never done", () => {
    expect(status("PR_CREATING", "pr", "Opening the pull request: refused")).toBe("failed");
  });

  it("a stage the server is inside is running", () => {
    expect(status("GENERATION_RUNNING", "generate")).toBe("running");
    expect(status("SECURITY_REVIEW_RUNNING", "review")).toBe("running");
    expect(status("VALIDATION_RUNNING", "validate")).toBe("running");
    expect(status("PR_CREATING", "pr")).toBe("running");
  });

  it("every stage is done only at COMPLETE", () => {
    expect(stageStatuses("COMPLETE", null).every((s) => s.status === "done")).toBe(true);
    expect(status("PR_APPROVED", "pr")).not.toBe("done");
  });
});

describe("generation", () => {
  it("does not consume a stored plan approval without a click", async () => {
    const approved = approval("TOOL_PLAN", { status: "APPROVED" });
    api.pipelineState.mockResolvedValue(runState("TOOL_PLAN_APPROVAL_PENDING", approved));
    renderPanel();

    expect(await screen.findByRole("button", { name: "Generate code" })).toBeInTheDocument();
    await waitFor(() => expect(api.agentPlan).toHaveBeenCalled());
    expectNoWrites();
  });

  it("generates on a click, with the stored approval, then shows the patch", async () => {
    const approved = approval("TOOL_PLAN", { status: "APPROVED" });
    api.pipelineState
      .mockResolvedValueOnce(runState("TOOL_PLAN_APPROVAL_PENDING", approved))
      .mockResolvedValue(runState("PATCH_READY"));
    api.pipelineGenerate.mockResolvedValue(step("PATCH_READY"));

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Generate code" }));

    expect(api.pipelineGenerate).toHaveBeenCalledWith("sess_1", "appr_plan");
    expect(await screen.findByRole("button", { name: "Run security review" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "src/webmcp/tools.ts" })).toBeInTheDocument();
    expect(screen.getByText("Registers the WebMCP tools.")).toBeInTheDocument();
    expect(within(stageBadge("Generate code")).getByText("Done")).toBeInTheDocument();
  });

  it("does not offer generation for an approval over a different plan", async () => {
    const stale = approval("TOOL_PLAN", { status: "APPROVED", artifact_hash: "older_plan_hash" });
    api.pipelineState.mockResolvedValue(runState("TOOL_PLAN_APPROVAL_PENDING", stale));
    renderPanel();

    expect(await screen.findByText(/covers a different version of the plan/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Generate code" })).not.toBeInTheDocument();
  });

  it("shows a 403 approval_required with the server's reason", async () => {
    const approved = approval("TOOL_PLAN", { status: "APPROVED" });
    api.pipelineState.mockResolvedValue(runState("TOOL_PLAN_APPROVAL_PENDING", approved));
    api.pipelineGenerate.mockRejectedValue(
      new ApiError(403, JSON.stringify({ detail: "TOOL_PLAN_APPROVED requires an approved TOOL_PLAN decision" })),
    );

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Generate code" }));

    expect(
      await screen.findByText(
        "Generating code failed (403): TOOL_PLAN_APPROVED requires an approved TOOL_PLAN decision",
      ),
    ).toBeVisible();
    expect(within(stageBadge("Generate code")).queryByText("Done")).not.toBeInTheDocument();
  });

  it("offers a retry for a generation that did not finish", async () => {
    api.pipelineState.mockResolvedValue(
      runState("GENERATION_RUNNING", null, "Generating the integration: binding refused"),
    );
    api.pipelineGenerate.mockResolvedValue(step("PATCH_READY"));
    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: "Retry generation" }));
    expect(api.pipelineGenerate).toHaveBeenCalledWith("sess_1");
    expect(within(stageBadge("Generate code")).getByText("Failed")).toBeInTheDocument();
  });
});

describe("security review", () => {
  it("runs the review through the real route on a click", async () => {
    api.pipelineState.mockResolvedValue(runState("PATCH_READY"));
    api.pipelineSecurityReview.mockResolvedValue(step("PATCH_APPROVAL_PENDING"));
    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: "Run security review" }));
    expect(api.pipelineSecurityReview).toHaveBeenCalledWith("sess_1");
  });

  it("renders a failed review as failed, never passed", async () => {
    api.pipelineState.mockResolvedValue(runState("SECURITY_REVIEW_FAILED"));
    api.pipelineSecurityReviewResult.mockResolvedValue(REVIEW_FAIL);
    renderPanel();

    await settled("Security review");
    const review = section("Security review");
    expect(within(review).getByText("Not passed")).toBeVisible();
    expect(within(review).queryByText("Passed the policy gate")).not.toBeInTheDocument();
    expect(within(review).getByText("Gate verdict: 1 HIGH finding blocks the gate.")).toBeVisible();
    expect(within(stageBadge("Security review")).getByText("Failed")).toBeInTheDocument();
    expect(within(stageBadge("Security review")).queryByText("Done")).not.toBeInTheDocument();
    // No way to approve a patch that failed review.
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate code" })).toBeInTheDocument();
  });

  it("shows each finding's severity, rule id and source, with model text labelled", async () => {
    api.pipelineState.mockResolvedValue(runState("SECURITY_REVIEW_FAILED"));
    api.pipelineSecurityReviewResult.mockResolvedValue(REVIEW_FAIL);
    renderPanel();

    const findings = await screen.findByRole("list", { name: "Security findings" });
    expect(within(findings).getByText("HIGH")).toBeVisible();
    expect(within(findings).getByText("xss.unescaped_html")).toBeVisible();
    expect(within(findings).getByText("source: model reviewer")).toBeVisible();
    expect(within(findings).getByText(/Model reviewer's note \(not a check result\)/)).toBeVisible();
    // The model's own verdict is advisory, and the override is stated.
    expect(screen.getByText(/The model reviewer said pass\. That is advisory/)).toBeVisible();
    expect(screen.getByText(/The gate overrode the model reviewer's view/)).toBeVisible();
  });

  it("renders an incomplete review as not passed, with its reason", async () => {
    api.pipelineState.mockResolvedValue(runState("SECURITY_REVIEW_FAILED"));
    api.pipelineSecurityReviewResult.mockResolvedValue(REVIEW_INCOMPLETE);
    renderPanel();

    expect(
      await screen.findByText("The security review did not complete: The reviewer returned no report."),
    ).toBeVisible();
    expect(within(section("Security review")).getByText("Not passed")).toBeVisible();
  });

  it("renders a missing review record as not passed", async () => {
    api.pipelineState.mockResolvedValue(runState("SECURITY_REVIEW_FAILED"));
    api.pipelineSecurityReviewResult.mockResolvedValue(null);
    renderPanel();

    expect(await screen.findByText("No security review is stored for this run.")).toBeVisible();
    expect(within(section("Security review")).getByText("Not passed")).toBeVisible();
  });

  it("a completed review the gate did not pass is not passed, whatever the model said", async () => {
    api.pipelineState.mockResolvedValue(runState("SECURITY_REVIEW_FAILED"));
    api.pipelineSecurityReviewResult.mockResolvedValue({ ...REVIEW_FAIL, findings: [] });
    renderPanel();

    await settled("Security review");
    expect(within(section("Security review")).getByText("Not passed")).toBeVisible();
  });

  it("regenerates after a failed review only on a click, without an approval id", async () => {
    api.pipelineState.mockResolvedValue(runState("SECURITY_REVIEW_FAILED"));
    api.pipelineSecurityReviewResult.mockResolvedValue(REVIEW_FAIL);
    api.pipelineGenerate.mockResolvedValue(step("PATCH_READY"));
    renderPanel();

    const button = await screen.findByRole("button", { name: "Regenerate code" });
    expect(api.pipelineGenerate).not.toHaveBeenCalled();
    await userEvent.click(button);
    expect(api.pipelineGenerate).toHaveBeenCalledWith("sess_1");
  });

  it("refuses a malformed review payload rather than rendering it", async () => {
    api.pipelineState.mockResolvedValue(runState("SECURITY_REVIEW_FAILED"));
    api.pipelineSecurityReviewResult.mockResolvedValue({
      ...REVIEW_PASS,
      passed: "yes",
    } as unknown as SecurityReviewDto);
    renderPanel();

    expect(
      await screen.findByText(/The stored security review is not in the shape this screen reads/),
    ).toBeVisible();
    expect(screen.queryByText("Passed the policy gate")).not.toBeInTheDocument();
  });
});

describe("patch approval", () => {
  it("shows the review, the diff and the card bound to the patch hash", async () => {
    api.pipelineState.mockResolvedValue(runState("PATCH_APPROVAL_PENDING", approval("PATCH")));
    renderPanel();

    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(within(section("Security review")).getByText("Passed the policy gate")).toBeVisible();
    expect(screen.getByText("source: policy engine")).toBeVisible();
    expect(screen.getByRole("region", { name: /Approve the generated patch/ })).toHaveTextContent(
      `version ${PATCH_HASH.slice(0, 12)}`,
    );
    expect(screen.getByRole("button", { name: "src/webmcp/tools.ts" })).toBeInTheDocument();
    expect(screen.getByText("+export const a = 1;")).toBeInTheDocument();
  });

  it("decides nothing without a click", async () => {
    api.pipelineState.mockResolvedValue(runState("PATCH_APPROVAL_PENDING", approval("PATCH")));
    renderPanel();

    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    await waitFor(() => expect(api.pipelinePatch).toHaveBeenCalled());
    expectNoWrites();
    expect(within(stageBadge("Approve patch")).getByText("Current")).toBeInTheDocument();
  });

  it("disables approving an approval over a different patch", async () => {
    const stale = approval("PATCH", { artifact_hash: "an_older_patch_hash" });
    api.pipelineState.mockResolvedValue(runState("PATCH_APPROVAL_PENDING", stale));
    renderPanel();

    expect(await screen.findByText(/This has changed since the decision was requested/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("approves on a click, then validates only on a second click", async () => {
    const approved = approval("PATCH", { status: "APPROVED", decided_at: "2026-09-12T10:02:00Z" });
    api.pipelineState
      .mockResolvedValueOnce(runState("PATCH_APPROVAL_PENDING", approval("PATCH")))
      .mockResolvedValue(runState("PATCH_APPROVAL_PENDING", approved));
    api.decideApproval.mockResolvedValue(approved);
    api.pipelineValidate.mockResolvedValue(step("VALIDATION_PASSED"));

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(api.decideApproval).toHaveBeenCalledWith("appr_patch", "APPROVED");

    const run = await screen.findByRole("button", { name: "Run validation" });
    expect(api.pipelineValidate).not.toHaveBeenCalled();
    await userEvent.click(run);
    expect(api.pipelineValidate).toHaveBeenCalledWith("sess_1", "appr_patch");
  });

  it("shows a 503 on validation as the executor's reason", async () => {
    const approved = approval("PATCH", { status: "APPROVED" });
    api.pipelineState.mockResolvedValue(runState("PATCH_APPROVAL_PENDING", approved));
    api.pipelineValidate.mockRejectedValue(
      new ApiError(
        503,
        JSON.stringify({ detail: "No secure executor. Secure executor not attached: no network namespaces" }),
      ),
    );

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Run validation" }));

    expect(
      await screen.findByText(
        "Executor unavailable: No secure executor. Secure executor not attached: no network namespaces",
      ),
    ).toBeVisible();
  });

  it("rejects on a click and returns the run to the tool plan", async () => {
    const rejected = approval("PATCH", { status: "REJECTED" });
    api.pipelineState
      .mockResolvedValueOnce(runState("PATCH_APPROVAL_PENDING", approval("PATCH")))
      .mockResolvedValue(runState("TOOL_PLAN_APPROVAL_PENDING", approval("TOOL_PLAN")));
    api.decideApproval.mockResolvedValue(rejected);
    api.pipelineReject.mockResolvedValue(step("TOOL_PLAN_APPROVAL_PENDING"));

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));

    expect(api.decideApproval).toHaveBeenCalledWith("appr_patch", "REJECTED");
    await waitFor(() => expect(api.pipelineReject).toHaveBeenCalledWith("sess_1", "appr_patch"));
    expect(await screen.findByRole("heading", { name: "Tool plan" })).toBeInTheDocument();
  });

  it("shows no card when the patch read fails, and says why", async () => {
    api.pipelineState.mockResolvedValue(runState("PATCH_APPROVAL_PENDING", approval("PATCH")));
    api.pipelinePatch.mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "The stored patch no longer matches its hash." })),
    );
    renderPanel();

    expect(
      await screen.findByText(
        "Reading the stored results failed (409): The stored patch no longer matches its hash.",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});

describe("validation and readiness", () => {
  it("renders a failed check as failed and the validation as not passed", async () => {
    api.pipelineState.mockResolvedValue(runState("VALIDATION_FAILED"));
    api.pipelineValidationResult.mockResolvedValue(VALIDATION_FAIL);
    renderPanel();

    await settled("Validation");
    const validation = section("Validation");
    expect(within(verdict("Validation")).getByText("Not passed")).toBeVisible();
    expect(within(verdict("Validation")).queryByText("Passed")).not.toBeInTheDocument();
    const build = within(validation).getByRole("row", { name: "Check build" });
    expect(within(build).getByText("Failed")).toBeVisible();
    expect(within(build).getByText(/exit 2/)).toBeVisible();
    expect(within(validation).getByText("Check failed: build")).toBeVisible();
    expect(within(stageBadge("Validation")).getByText("Failed")).toBeInTheDocument();
    expect(within(stageBadge("Validation")).queryByText("Done")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request pull request" })).not.toBeInTheDocument();
  });

  it("shows the readiness score with each component's reason", async () => {
    api.pipelineState.mockResolvedValue(runState("VALIDATION_FAILED"));
    api.pipelineValidationResult.mockResolvedValue(VALIDATION_FAIL);
    renderPanel();

    const readiness = await screen.findByRole("group", { name: "Agent readiness" });
    expect(readiness).toHaveTextContent("60 / 100");
    expect(within(readiness).getByText("1 tool registered and discovered")).toBeVisible();
    expect(within(readiness).getByText("no regression suite was executed")).toBeVisible();
  });

  it("renders a validation that could not run as not passed, with no score", async () => {
    api.pipelineState.mockResolvedValue(runState("VALIDATION_FAILED"));
    api.pipelineValidationResult.mockResolvedValue(VALIDATION_COULD_NOT_RUN);
    renderPanel();

    expect(
      await screen.findByText("Validation could not run: npm ci failed: lockfile missing"),
    ).toBeVisible();
    expect(screen.getByText(/No readiness score/)).toBeVisible();
    expect(within(section("Validation")).getByText("Not passed")).toBeVisible();
  });

  it("renders an unexecuted tool check as a blocking issue, not a pass", async () => {
    api.pipelineState.mockResolvedValue(runState("VALIDATION_FAILED"));
    api.pipelineValidationResult.mockResolvedValue({
      ...VALIDATION_PASS,
      passed: false,
      validated: false,
      unexecuted_tool_checks: ["tool_discovery"],
    });
    renderPanel();

    expect(await screen.findByText("Required check did not run: tool_discovery")).toBeVisible();
    expect(within(section("Validation")).getByText("Not passed")).toBeVisible();
  });

  it("shows a skipped check with its reason, and a passed validation as passed", async () => {
    api.pipelineState.mockResolvedValue(runState("VALIDATION_PASSED"));
    renderPanel();

    await settled("Validation");
    const skipped = screen.getByRole("row", { name: "Check regression" });
    expect(within(skipped).getByText("Skipped")).toBeVisible();
    expect(within(skipped).getByText("Not run: The repository defines no test script.")).toBeVisible();
    expect(within(verdict("Validation")).getByText("Passed")).toBeVisible();
    expect(within(stageBadge("Validation")).getByText("Done")).toBeInTheDocument();
    // No trust claim is made from the validation read.
    expect(screen.queryByText(/hardware-attested|Hardware-backed/i)).not.toBeInTheDocument();
  });

  it("shows the server's refusal when the project cannot open a pull request", async () => {
    api.pipelineState.mockResolvedValue(runState("VALIDATION_PASSED"));
    api.pipelineRequestPullRequest.mockRejectedValue(
      new ApiError(403, JSON.stringify({ detail: "This project is READ_ONLY; writing needs WRITE_PR." })),
    );
    renderPanel();

    await userEvent.click(await screen.findByRole("button", { name: "Request pull request" }));
    expect(
      await screen.findByText(
        "Requesting the pull request failed (403): This project is READ_ONLY; writing needs WRITE_PR.",
      ),
    ).toBeVisible();
  });
});

describe("pull request", () => {
  it("needs typed confirmation, decides nothing on mount, and is bound to the patch hash", async () => {
    api.pipelineState.mockResolvedValue(runState("PR_APPROVAL_PENDING", approval("PULL_REQUEST")));
    api.pipelinePullRequest.mockResolvedValue({ ...PR_OPENED, status: "AWAITING_APPROVAL", url: null, number: null, branch: null });
    renderPanel();

    const approve = await screen.findByRole("button", { name: "Approve" });
    expect(approve).toBeDisabled();
    expect(screen.getByRole("region", { name: /Open a pull request/ })).toHaveTextContent(
      `version ${PATCH_HASH.slice(0, 12)}`,
    );
    await waitFor(() => expect(api.pipelinePullRequest).toHaveBeenCalled());
    expectNoWrites();
  });

  it("approves after typing, then opens the PR only on a second click", async () => {
    const approved = approval("PULL_REQUEST", { status: "APPROVED" });
    api.pipelineState
      .mockResolvedValueOnce(runState("PR_APPROVAL_PENDING", approval("PULL_REQUEST")))
      .mockResolvedValue(runState("PR_APPROVAL_PENDING", approved));
    api.decideApproval.mockResolvedValue(approved);
    api.pipelineCreatePullRequest.mockResolvedValue(step("COMPLETE"));

    renderPanel();
    await userEvent.type(await screen.findByLabelText(/Type/), "approve");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(api.decideApproval).toHaveBeenCalledWith("appr_pr", "APPROVED");

    const open = await screen.findByRole("button", { name: "Open pull request" });
    expect(api.pipelineCreatePullRequest).not.toHaveBeenCalled();
    await userEvent.click(open);
    expect(api.pipelineCreatePullRequest).toHaveBeenCalledWith("sess_1", "appr_pr");
  });

  it("renders a failed PR write as failed, never opened", async () => {
    api.pipelineState.mockResolvedValue(
      runState("PR_CREATING", null, "Opening the pull request: the base branch moved"),
    );
    api.pipelinePullRequest.mockResolvedValue(PR_FAILED);
    api.pipelineCreatePullRequest.mockResolvedValue(step("PR_CREATING"));
    renderPanel();

    await settled("Pull request");
    const pr = section("Pull request");
    expect(within(pr).getByText("Failed")).toBeVisible();
    expect(within(pr).queryByText("Opened")).not.toBeInTheDocument();
    expect(
      within(pr).getByText("Opening the pull request failed: Opening the pull request: the base branch moved"),
    ).toBeVisible();
    expect(within(stageBadge("Pull request")).getByText("Failed")).toBeInTheDocument();
    expect(within(stageBadge("Pull request")).queryByText("Done")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Retry opening the pull request" }));
    expect(api.pipelineCreatePullRequest).toHaveBeenCalledWith("sess_1");
  });

  it("shows the branch, the PR URL as a link, and its status", async () => {
    api.pipelineState.mockResolvedValue(runState("COMPLETE"));
    api.pipelinePullRequest.mockResolvedValue(PR_OPENED);
    renderPanel();

    const link = await screen.findByRole("link", { name: PR_OPENED.url! });
    expect(link).toHaveAttribute("href", PR_OPENED.url);
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    const pr = section("Pull request");
    expect(within(pr).getByText("Opened")).toBeVisible();
    expect(within(pr).getByText("mcpforge/sess_1")).toBeVisible();
    expect(within(pr).getByText("#7")).toBeVisible();
    expect(within(stageBadge("Pull request")).getByText("Done")).toBeInTheDocument();
  });

  it("never links a recorded URL that is not https", async () => {
    expect(safeHttpsUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpsUrl("http://github.com/x")).toBeNull();
    api.pipelineState.mockResolvedValue(runState("COMPLETE"));
    api.pipelinePullRequest.mockResolvedValue({ ...PR_OPENED, url: "javascript:alert(1)" });
    renderPanel();

    expect(await screen.findByText(/not a link: not an https URL/)).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("the whole journey against a stateful fake backend", () => {
  it("reaches PR_CREATED from the UI alone, one click per decision", async () => {
    let state: RunState = "TOOL_PLAN_APPROVAL_PENDING";
    const approvals: Record<string, ApprovalDto> = { appr_plan: approval("TOOL_PLAN") };
    let open: string | null = "appr_plan";
    const visited: RunState[] = [];

    const refuse = () =>
      new ApiError(403, JSON.stringify({ detail: "No approved decision for this gate." }));
    const requireApproved = (id: string | undefined, gate: ApprovalGate) => {
      const a = id ? approvals[id] : undefined;
      if (!a || a.gate !== gate || a.status !== "APPROVED") throw refuse();
    };
    const move = (to: RunState) => {
      state = to;
      visited.push(to);
    };

    api.pipelineState.mockImplementation(async () =>
      runState(state, open ? (approvals[open] ?? null) : null),
    );
    api.decideApproval.mockImplementation(async (id, decision) => {
      approvals[id] = { ...approvals[id]!, status: decision, decided_at: "2026-09-12T11:00:00Z" };
      return approvals[id]!;
    });
    api.pipelineGenerate.mockImplementation(async (_s, id) => {
      requireApproved(id, "TOOL_PLAN");
      ["TOOL_PLAN_APPROVED", "GENERATION_RUNNING", "PATCH_READY"].forEach((s) => move(s as RunState));
      open = null;
      return step(state);
    });
    api.pipelineSecurityReview.mockImplementation(async () => {
      ["SECURITY_REVIEW_RUNNING", "SECURITY_REVIEW_PASSED", "PATCH_APPROVAL_PENDING"].forEach((s) =>
        move(s as RunState),
      );
      approvals.appr_patch = approval("PATCH");
      open = "appr_patch";
      return step(state);
    });
    api.pipelineValidate.mockImplementation(async (_s, id) => {
      requireApproved(id, "PATCH");
      ["PATCH_APPROVED", "VALIDATION_RUNNING", "VALIDATION_PASSED"].forEach((s) => move(s as RunState));
      open = null;
      return step(state);
    });
    api.pipelineRequestPullRequest.mockImplementation(async () => {
      move("PR_APPROVAL_PENDING");
      approvals.appr_pr = approval("PULL_REQUEST");
      open = "appr_pr";
      return step(state);
    });
    api.pipelineCreatePullRequest.mockImplementation(async (_s, id) => {
      requireApproved(id, "PULL_REQUEST");
      ["PR_APPROVED", "PR_CREATING", "PR_CREATED", "COMPLETE"].forEach((s) => move(s as RunState));
      open = null;
      return { ...step(state), pull_request_url: PR_OPENED.url };
    });
    api.pipelinePullRequest.mockImplementation(async () =>
      state === "COMPLETE"
        ? PR_OPENED
        : state === "PR_APPROVAL_PENDING"
          ? { status: "AWAITING_APPROVAL", url: null, number: null, branch: null, failure: null }
          : null,
    );

    renderPanel();
    const user = userEvent.setup();

    // Plan decision, then generation.
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await user.click(await screen.findByRole("button", { name: "Generate code" }));
    // Review.
    await user.click(await screen.findByRole("button", { name: "Run security review" }));
    // Patch decision, then validation.
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    await user.click(await screen.findByRole("button", { name: "Run validation" }));
    // Pull request: request, typed approval, open.
    await user.click(await screen.findByRole("button", { name: "Request pull request" }));
    await user.type(await screen.findByLabelText(/Type/), "approve");
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(await screen.findByRole("button", { name: "Open pull request" }));

    expect(await screen.findByRole("link", { name: PR_OPENED.url! })).toBeInTheDocument();
    expect(screen.getByText("run state COMPLETE")).toBeInTheDocument();
    expect(visited).toContain("PR_CREATED");
    expect(stageStatuses("COMPLETE", null).every((s) => s.status === "done")).toBe(true);
    expect(within(stageBadge("Pull request")).getByText("Done")).toBeInTheDocument();

    // Every gate was decided by a click, once, and every consumption named it.
    expect(api.decideApproval.mock.calls).toEqual([
      ["appr_plan", "APPROVED"],
      ["appr_patch", "APPROVED"],
      ["appr_pr", "APPROVED"],
    ]);
    expect(api.pipelineGenerate).toHaveBeenCalledWith("sess_1", "appr_plan");
    expect(api.pipelineValidate).toHaveBeenCalledWith("sess_1", "appr_patch");
    expect(api.pipelineCreatePullRequest).toHaveBeenCalledWith("sess_1", "appr_pr");
    expect(api.pipelineReject).not.toHaveBeenCalled();
  }, 20000);
});
