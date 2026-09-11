import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JourneyPanel, type JourneyApi } from "@/components/pipeline/journey-panel";
import { stageStatuses } from "@/components/pipeline/journey";
import { ApiError } from "@/lib/api/client";
import type {
  AgentArtifactDto,
  ApprovalDto,
  PipelineStepDto,
  RunState,
  RunStateDto,
} from "@/lib/api/types";

/**
 * The journey panel — ticket T5a. The API client is replaced at its boundary
 * (the injected `api`); every payload below has the shape the backend stores.
 */

function runState(state: RunState, over: Partial<RunStateDto> = {}): RunStateDto {
  return {
    session_id: "sess_1",
    project_id: "proj_1",
    state,
    updated_at: "2026-09-12T10:00:00Z",
    pending_gate: null,
    failure: null,
    ...over,
  };
}

function step(state: RunState): PipelineStepDto {
  return { session_id: "sess_1", state, detail: "ok", approval: null, pull_request_url: null };
}

const ANALYSIS: AgentArtifactDto = {
  available: true,
  kind: "ANALYSIS",
  artifact_hash: "an_hash_0001",
  payload: {
    analysis: {
      framework: "nextjs",
      summary: "Hotel app",
      workflows: [
        {
          id: "search_hotels",
          name: "Search hotels",
          description: "Find hotels by city.",
          risk: "READ",
          primary_function: "searchHotels",
          evidence: [{ path: "src/lib/hotels.ts", symbol: "searchHotels", line: 3 }],
          confidence: 0.9,
        },
        {
          id: "cancel_reservation",
          name: "Cancel reservation",
          description: "Cancel a booking.",
          risk: "DESTRUCTIVE",
          primary_function: "cancelReservation",
          evidence: [{ path: "src/lib/bookings.ts", symbol: null, line: null }],
          confidence: 0.8,
        },
        {
          id: "guess_prices",
          name: "Guess prices",
          description: "Weakly evidenced.",
          risk: "READ",
          primary_function: "guessPrices",
          evidence: [{ path: "src/lib/prices.ts" }],
          confidence: 0.3,
        },
      ],
    },
  },
};

const PLAN: AgentArtifactDto = {
  available: true,
  kind: "TOOL_PLAN",
  artifact_hash: "plan_hash_0001abcd",
  payload: {
    plan: {
      tools: [
        {
          name: "search_hotels",
          title: "Search hotels",
          description: "Search hotels by city.",
          workflow_id: "search_hotels",
          maps_to_function: "searchHotels",
          parameters: [
            { name: "city", json_type: "string", description: "City name", required: true },
          ],
          output_description: "A list of hotels",
          risk: "READ",
          evidence: [{ path: "src/lib/hotels.ts", symbol: "searchHotels", line: 3 }],
          approval_required: false,
        },
        {
          name: "cancel_reservation",
          title: "Cancel reservation",
          description: "Cancel a booking.",
          workflow_id: "cancel_reservation",
          maps_to_function: "cancelReservation",
          parameters: [],
          output_description: "The cancelled booking",
          risk: "DESTRUCTIVE",
          evidence: [{ path: "src/lib/bookings.ts", symbol: null, line: null }],
          approval_required: true,
        },
      ],
      notes: [],
    },
    risk_discrepancies: [],
    types_not_checked: { cancel_reservation: ["BookingRef"] },
  },
};

function approval(over: Partial<ApprovalDto> = {}): ApprovalDto {
  return {
    id: "appr_plan",
    gate: "TOOL_PLAN",
    artifact_hash: "plan_hash_0001abcd",
    summary: "Approve the WebMCP tool plan: search_hotels, cancel_reservation",
    status: "PENDING",
    requested_at: "2026-09-12T10:00:00Z",
    decided_at: null,
    actor_uid: null,
    ...over,
  };
}

function atPlanGate(a: ApprovalDto | null = approval()): RunStateDto {
  return runState("TOOL_PLAN_APPROVAL_PENDING", {
    pending_gate: {
      gate: "TOOL_PLAN",
      artifact_kind: "TOOL_PLAN",
      artifact_hash: "plan_hash_0001abcd",
      approval: a,
    },
  });
}

function makeApi() {
  return {
    pipelineState: vi.fn<JourneyApi["pipelineState"]>(),
    pipelineConnect: vi.fn<JourneyApi["pipelineConnect"]>(),
    pipelineAnalyze: vi.fn<JourneyApi["pipelineAnalyze"]>(),
    pipelineSelectWorkflows: vi.fn<JourneyApi["pipelineSelectWorkflows"]>(),
    pipelinePlan: vi.fn<JourneyApi["pipelinePlan"]>(),
    pipelineReject: vi.fn<JourneyApi["pipelineReject"]>(),
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

describe("stage status comes from run state only", () => {
  it("never marks the approval stage done before TOOL_PLAN_APPROVED", () => {
    const at = stageStatuses("TOOL_PLAN_APPROVAL_PENDING", null);
    expect(at.find((s) => s.stage.id === "approve")?.status).toBe("current");
    expect(at.find((s) => s.stage.id === "plan")?.status).toBe("done");
    expect(at.find((s) => s.stage.id === "generate")?.status).toBe("not_yet");
  });

  it("marks the current stage failed when the server reports a failure", () => {
    const at = stageStatuses("ANALYSIS_RUNNING", "Repository analysis: boom");
    expect(at.find((s) => s.stage.id === "analyze")?.status).toBe("failed");
  });
});

describe("connect and analyze", () => {
  it("connects through the real route and re-reads state afterwards", async () => {
    api.pipelineState
      .mockResolvedValueOnce(runState("PROJECT_CREATED"))
      .mockResolvedValue(runState("ANALYSIS_PENDING"));
    api.pipelineConnect.mockResolvedValue(step("ANALYSIS_PENDING"));

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Connect repository" }));

    expect(api.pipelineConnect).toHaveBeenCalledWith("sess_1");
    expect(await screen.findByRole("button", { name: "Analyze repository" })).toBeInTheDocument();
    expect(within(stageBadge("Connect repository")).getByText("Done")).toBeInTheDocument();
    expect(api.pipelineState.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("does not render a stage done because a POST returned; state decides", async () => {
    api.pipelineState.mockResolvedValue(runState("ANALYSIS_PENDING"));
    // The POST claims success, but the server state has not moved.
    api.pipelineAnalyze.mockResolvedValue(step("WORKFLOW_SELECTION_PENDING"));

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Analyze repository" }));

    await waitFor(() => expect(api.pipelineAnalyze).toHaveBeenCalled());
    expect(within(stageBadge("Analyze")).getByText("Current")).toBeInTheDocument();
    expect(within(stageBadge("Analyze")).queryByText("Done")).not.toBeInTheDocument();
  });

  it("renders a failed step as failed, with the server's detail", async () => {
    api.pipelineState
      .mockResolvedValueOnce(runState("ANALYSIS_PENDING"))
      .mockResolvedValue(
        runState("ANALYSIS_RUNNING", { failure: "Repository analysis: framework unsupported" }),
      );
    api.pipelineAnalyze.mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "framework unsupported" })),
    );

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Analyze repository" }));

    expect(await screen.findByText("Analysis failed (409): framework unsupported")).toBeVisible();
    expect(screen.getByText("Failed: Repository analysis: framework unsupported")).toBeVisible();
    expect(within(stageBadge("Analyze")).getByText("Failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry analysis" })).toBeInTheDocument();
  });

  it("shows a 503 as executor unavailable with its reason", async () => {
    api.pipelineState.mockResolvedValue(runState("ANALYSIS_PENDING"));
    api.pipelineAnalyze.mockRejectedValue(
      new ApiError(
        503,
        JSON.stringify({ detail: "Secure executor not attached: docker is not running" }),
      ),
    );

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Analyze repository" }));

    expect(
      await screen.findByText(
        "Executor unavailable: Secure executor not attached: docker is not running",
      ),
    ).toBeVisible();
  });

  it("shows a failed state read as a failure, not a stale stage", async () => {
    api.pipelineState.mockRejectedValue(new ApiError(404, JSON.stringify({ detail: "Session not found" })));
    renderPanel();
    expect(
      await screen.findByText("Reading the run state failed (404): Session not found"),
    ).toBeVisible();
    expect(screen.queryByRole("list", { name: "Journey stages" })).not.toBeInTheDocument();
  });
});

describe("workflow selection", () => {
  it("lists workflows with name, risk and description", async () => {
    api.pipelineState.mockResolvedValue(runState("WORKFLOW_SELECTION_PENDING"));
    renderPanel();

    expect(await screen.findByLabelText("Search hotels")).toBeInTheDocument();
    expect(screen.getByText("Find hotels by city.")).toBeInTheDocument();
    expect(screen.getByText("DESTRUCTIVE · Human approval")).toBeInTheDocument();
    expect(screen.getByText("low confidence")).toBeInTheDocument();
    // Low confidence is not preselected (04_FRONTEND_SPEC.md §7).
    expect(screen.getByLabelText("Guess prices")).not.toBeChecked();
    expect(screen.getByLabelText("Search hotels")).toBeChecked();
  });

  it("sends exactly the chosen ids", async () => {
    api.pipelineState
      .mockResolvedValueOnce(runState("WORKFLOW_SELECTION_PENDING"))
      .mockResolvedValue(runState("WORKFLOWS_SELECTED"));
    api.pipelineSelectWorkflows.mockResolvedValue(step("WORKFLOWS_SELECTED"));

    renderPanel();
    await userEvent.click(await screen.findByLabelText("Cancel reservation")); // deselect
    await userEvent.click(screen.getByLabelText("Guess prices")); // select
    await userEvent.click(screen.getByRole("button", { name: /Use 2 selected workflows/ }));

    expect(api.pipelineSelectWorkflows).toHaveBeenCalledTimes(1);
    expect(api.pipelineSelectWorkflows).toHaveBeenCalledWith("sess_1", {
      workflow_ids: ["search_hotels", "guess_prices"],
    });
    expect(await screen.findByRole("button", { name: "Design tool plan" })).toBeInTheDocument();
  });

  it("cannot submit an empty selection", async () => {
    api.pipelineState.mockResolvedValue(runState("WORKFLOW_SELECTION_PENDING"));
    renderPanel();
    await userEvent.click(await screen.findByLabelText("Search hotels"));
    await userEvent.click(screen.getByLabelText("Cancel reservation"));
    expect(screen.getByRole("button", { name: /Use 0 selected/ })).toBeDisabled();
  });
});

describe("tool plan and its approval", () => {
  it("triggers the plan through the real route", async () => {
    api.pipelineState
      .mockResolvedValueOnce(runState("WORKFLOWS_SELECTED"))
      .mockResolvedValue(atPlanGate());
    api.pipelinePlan.mockResolvedValue(step("TOOL_PLAN_APPROVAL_PENDING"));

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Design tool plan" }));

    expect(api.pipelinePlan).toHaveBeenCalledWith("sess_1");
    expect(await screen.findByRole("heading", { name: "search_hotels" })).toBeInTheDocument();
  });

  it("shows names, descriptions, inputs, risk, gating and unchecked types", async () => {
    api.pipelineState.mockResolvedValue(atPlanGate());
    renderPanel();

    expect(await screen.findByRole("heading", { name: "cancel_reservation" })).toBeInTheDocument();
    expect(screen.getByText("Search hotels by city.")).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Input schema for search_hotels" })).toBeInTheDocument();
    expect(screen.getByText("City name")).toBeInTheDocument();
    expect(screen.getByText("gated: asks a human before running")).toBeInTheDocument();
    expect(screen.getByText("not gated")).toBeInTheDocument();
    expect(screen.getByText("not type-checked")).toBeVisible();
    expect(screen.getByText("BookingRef")).toBeVisible();
    expect(screen.getByRole("note")).toHaveTextContent("cancel_reservation");
  });

  it("decides nothing without a click", async () => {
    api.pipelineState.mockResolvedValue(atPlanGate());
    renderPanel();

    expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
    // Let a poll-free render settle, then confirm no decision was sent.
    await waitFor(() => expect(api.agentPlan).toHaveBeenCalled());
    expect(api.decideApproval).not.toHaveBeenCalled();
    expect(api.pipelineReject).not.toHaveBeenCalled();
    expect(within(stageBadge("Approve tool plan")).getByText("Current")).toBeInTheDocument();
  });

  it("approves on a click, and keeps the stage not done until state says so", async () => {
    const approved = approval({ status: "APPROVED", decided_at: "2026-09-12T10:01:00Z" });
    api.pipelineState.mockResolvedValueOnce(atPlanGate()).mockResolvedValue(atPlanGate(approved));
    api.decideApproval.mockResolvedValue(approved);

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));

    expect(api.decideApproval).toHaveBeenCalledWith("appr_plan", "APPROVED");
    expect(await screen.findByText(/Your approval is stored/)).toBeInTheDocument();
    expect(within(stageBadge("Approve tool plan")).queryByText("Done")).not.toBeInTheDocument();
  });

  it("marks approval done only when state reaches TOOL_PLAN_APPROVED", async () => {
    api.pipelineState.mockResolvedValue(runState("TOOL_PLAN_APPROVED"));
    renderPanel();
    await waitFor(() =>
      expect(within(stageBadge("Approve tool plan")).getByText("Done")).toBeInTheDocument(),
    );
    expect(within(stageBadge("Generate code")).getByText("Not yet")).toBeInTheDocument();
  });

  it("rejects on a click and applies the rejection through the pipeline", async () => {
    const rejected = approval({ status: "REJECTED", decided_at: "2026-09-12T10:01:00Z" });
    api.pipelineState
      .mockResolvedValueOnce(atPlanGate())
      .mockResolvedValue(runState("WORKFLOW_SELECTION_PENDING"));
    api.decideApproval.mockResolvedValue(rejected);
    api.pipelineReject.mockResolvedValue(step("WORKFLOW_SELECTION_PENDING"));

    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));

    expect(api.decideApproval).toHaveBeenCalledWith("appr_plan", "REJECTED");
    await waitFor(() => expect(api.pipelineReject).toHaveBeenCalledWith("sess_1", "appr_plan"));
    expect(await screen.findByLabelText("Search hotels")).toBeInTheDocument();
  });

  it("does not show the approval card when the plan cannot be read", async () => {
    api.pipelineState.mockResolvedValue(atPlanGate());
    api.agentPlan.mockResolvedValue({ ...PLAN, payload: { plan: { tools: "nope" } } });
    renderPanel();

    expect(
      await screen.findByText(/The stored tool plan is not in the shape this screen reads/),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});
