import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ApprovalCard } from "@/components/approval/approval-card";
import type { ApprovalDto } from "@/lib/api/types";

function approval(over: Partial<ApprovalDto> = {}): ApprovalDto {
  return {
    id: "appr_1",
    gate: "TOOL_PLAN",
    artifact_hash: "abc123def456789",
    summary: "4 WebMCP tools across 5 files",
    status: "PENDING",
    requested_at: new Date().toISOString(),
    decided_at: null,
    actor_uid: null,
    ...over,
  };
}

describe("approval card", () => {
  it("states exactly what approving permits", () => {
    render(<ApprovalCard approval={approval()} onDecide={vi.fn()} />);
    expect(screen.getByText(/lets MCPForge generate code/i)).toBeInTheDocument();
    expect(screen.getByText(/No repository changes yet/i)).toBeInTheDocument();
  });

  it("warns that a pull request never touches the default branch", () => {
    render(<ApprovalCard approval={approval({ gate: "PULL_REQUEST" })} onDecide={vi.fn()} />);
    expect(screen.getByText(/default branch is untouched/i)).toBeInTheDocument();
  });

  it("shows which version of the artifact it covers", () => {
    render(<ApprovalCard approval={approval()} onDecide={vi.fn()} />);
    expect(screen.getByText(/version abc123def456/)).toBeInTheDocument();
  });

  it("sends the decision when approved", async () => {
    const onDecide = vi.fn().mockResolvedValue(undefined);
    render(<ApprovalCard approval={approval()} onDecide={onDecide} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).toHaveBeenCalledWith("APPROVED");
  });

  it("sends the decision when rejected", async () => {
    const onDecide = vi.fn().mockResolvedValue(undefined);
    render(<ApprovalCard approval={approval()} onDecide={onDecide} />);
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(onDecide).toHaveBeenCalledWith("REJECTED");
  });

  it("disables approving when the artifact changed since the request", async () => {
    const onDecide = vi.fn();
    render(
      <ApprovalCard
        approval={approval()}
        onDecide={onDecide}
        currentArtifactHash="a-different-hash"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/has changed/i);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).not.toHaveBeenCalled();
  });

  it("stays enabled when the artifact still matches", () => {
    render(
      <ApprovalCard
        approval={approval()}
        onDecide={vi.fn()}
        currentArtifactHash="abc123def456789"
      />,
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the server's decision and offers no buttons once decided", () => {
    render(
      <ApprovalCard
        approval={approval({ status: "APPROVED", decided_at: new Date().toISOString() })}
        onDecide={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.getByText(/^Approved/)).toBeInTheDocument();
  });

  it("does not render as approved before the server confirms", async () => {
    // The card reflects server state only; a click alone never flips it.
    let resolve: () => void = () => {};
    const pending = new Promise<void>((r) => {
      resolve = r;
    });
    const onDecide = vi.fn().mockReturnValue(pending);

    render(<ApprovalCard approval={approval()} onDecide={onDecide} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(screen.getByText("Awaiting your decision")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    resolve();
  });

  it("shows the real error when a decision is refused by the server", async () => {
    const onDecide = vi.fn().mockRejectedValue(new Error("409 already decided"));
    render(<ApprovalCard approval={approval()} onDecide={onDecide} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/409 already decided/)).toBeInTheDocument();
  });

  it("is fully keyboard operable", async () => {
    const onDecide = vi.fn().mockResolvedValue(undefined);
    render(<ApprovalCard approval={approval()} onDecide={onDecide} />);
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "Approve" })).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(onDecide).toHaveBeenCalledWith("APPROVED");
  });
});
