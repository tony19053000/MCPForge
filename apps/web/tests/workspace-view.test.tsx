import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ApprovalDto, EventDto } from "@/lib/api/types";

/**
 * The workspace as a mounted component — round-2 review finding 2.
 *
 * The polling effect that makes an agent's work visible was added with no test
 * that renders `WorkspaceView`, so deleting it left every gate green while the
 * agent's approvals and timeline disappeared. That is the third recurrence of
 * this class on this project (Phase 2's chat UI, Phase 7's dead adapter). Every
 * assertion here must fail with the effect removed.
 */

const listEvents = vi.fn<() => Promise<EventDto[]>>();
const getApproval = vi.fn<() => Promise<ApprovalDto>>();
const decideApproval = vi.fn<() => Promise<ApprovalDto>>();

const api = {
  listProjects: vi.fn(async () => [
    {
      id: "proj_1",
      name: "hotel app",
      access_mode: "READ_ONLY" as const,
      repository_full_name: null,
      is_demo: false,
    },
  ]),
  createProject: vi.fn(),
  createSession: vi.fn(async () => ({
    id: "sess_1",
    project_id: "proj_1",
    title: "run",
    state: "PROJECT_CREATED" as const,
  })),
  listRepositories: vi.fn(async () => []),
  listEvents,
  getApproval,
  decideApproval,
  bindRepository: vi.fn(),
  elevateAccess: vi.fn(),
  revokeAccess: vi.fn(),
};

vi.mock("@/lib/auth/context", () => ({
  useAuth: () => ({
    session: { subject: "uid-owner", email: null, displayName: null, photoUrl: null, token: "t" },
    ready: true,
    availableProviders: ["google"],
    signIn: vi.fn(),
    signOut: vi.fn(),
    api,
  }),
}));

// The chat pane opens an SSE stream, which is not what this file is about.
vi.mock("@/components/workspace/chat", () => ({ Chat: () => <div>chat</div> }));

const { WorkspaceView } = await import("@/components/workspace/workspace-view");

function agentEvent(over: Partial<EventDto> = {}): EventDto {
  return {
    id: "evt_1",
    kind: "approval.requested",
    label: "Agent requested your decision: WORKFLOW_SELECTION",
    detail: { approval_id: "appr_1" },
    origin: "AGENT",
    created_at: "2026-09-03T10:00:00Z",
    ...over,
  };
}

function pendingApproval(over: Partial<ApprovalDto> = {}): ApprovalDto {
  return {
    id: "appr_1",
    gate: "WORKFLOW_SELECTION",
    artifact_hash: "abc12345",
    summary: "Expose 1 workflow as a WebMCP tool",
    status: "PENDING",
    requested_at: "2026-09-03T10:00:00Z",
    decided_at: null,
    actor_uid: null,
    ...over,
  };
}

beforeEach(() => {
  listEvents.mockReset().mockResolvedValue([]);
  getApproval.mockReset();
  decideApproval.mockReset();
});

afterEach(() => vi.clearAllMocks());

describe("the agent's work is visible to the developer", () => {
  it("polls the server timeline for the session", async () => {
    render(<WorkspaceView />);
    await waitFor(() => expect(listEvents).toHaveBeenCalledWith("sess_1"));
  });

  it("renders agent-origin events, labelled as the agent's", async () => {
    listEvents.mockResolvedValue([agentEvent()]);
    getApproval.mockResolvedValue(pendingApproval());

    render(<WorkspaceView />);

    await waitFor(() =>
      expect(screen.getByText(/Agent requested your decision/)).toBeInTheDocument(),
    );
    expect(screen.getByText("via agent")).toBeInTheDocument();
  });

  it("distinguishes a human decision from an agent request on the timeline", async () => {
    listEvents.mockResolvedValue([
      agentEvent(),
      agentEvent({
        id: "evt_2",
        kind: "approval.decided",
        label: "WORKFLOW_SELECTION: approved",
        origin: "HUMAN",
      }),
    ]);
    getApproval.mockResolvedValue(pendingApproval());

    render(<WorkspaceView />);

    await waitFor(() => expect(screen.getByText("you")).toBeInTheDocument());
    expect(screen.getByText("via agent")).toBeInTheDocument();
  });
});

describe("an approval an agent opened is reachable", () => {
  it("fetches the approval named by the event and shows the card", async () => {
    listEvents.mockResolvedValue([agentEvent()]);
    getApproval.mockResolvedValue(pendingApproval());

    render(<WorkspaceView />);

    await waitFor(() => expect(getApproval).toHaveBeenCalledWith("appr_1"));
    expect(await screen.findByText(/Expose 1 workflow/)).toBeInTheDocument();
  });

  it("can be decided by the developer, and only by them", async () => {
    listEvents.mockResolvedValue([agentEvent()]);
    getApproval.mockResolvedValue(pendingApproval());
    decideApproval.mockResolvedValue(
      pendingApproval({ status: "APPROVED", actor_uid: "uid-owner" }),
    );

    render(<WorkspaceView />);
    const approve = await screen.findByRole("button", { name: /approve/i });
    await userEvent.click(approve);

    await waitFor(() => expect(decideApproval).toHaveBeenCalledWith("appr_1", "APPROVED"));
  });

  it("does not present an already-decided approval as awaiting a decision", async () => {
    listEvents.mockResolvedValue([agentEvent()]);
    getApproval.mockResolvedValue(
      pendingApproval({ status: "APPROVED", actor_uid: "uid-owner", decided_at: "2026-09-03" }),
    );

    render(<WorkspaceView />);

    await waitFor(() => expect(getApproval).toHaveBeenCalled());
    expect(screen.getByText(/Nothing needs your decision/)).toBeInTheDocument();
    expect(screen.queryByText(/Expose 1 workflow/)).not.toBeInTheDocument();
  });

  it("shows nothing to decide when no approval was requested", async () => {
    listEvents.mockResolvedValue([
      agentEvent({ kind: "analysis.requested", label: "Agent requested repository analysis", detail: {} }),
    ]);

    render(<WorkspaceView />);

    await waitFor(() => expect(listEvents).toHaveBeenCalled());
    expect(getApproval).not.toHaveBeenCalled();
    expect(screen.getByText(/Nothing needs your decision/)).toBeInTheDocument();
  });
});

describe("polling failures", () => {
  it("keeps the workspace usable when a poll fails", async () => {
    listEvents.mockRejectedValue(new Error("network"));
    render(<WorkspaceView />);
    await waitFor(() => expect(listEvents).toHaveBeenCalled());
    // A failed poll retries; it must not take over the error surface, which is
    // reserved for failures the developer has to act on.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
