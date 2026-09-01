import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ActivityTimeline, isRenderableEvidence } from "@/components/workspace/activity-timeline";
import type { EventDto } from "@/lib/api/types";

function event(over: Partial<EventDto> = {}): EventDto {
  return {
    id: "e1",
    kind: "step.completed",
    label: "Scanning repository",
    detail: {},
    origin: "SYSTEM",
    created_at: new Date().toISOString(),
    ...over,
  };
}

describe("activity timeline", () => {
  it("shows a step label", () => {
    render(<ActivityTimeline events={[event()]} />);
    expect(screen.getByText("Scanning repository")).toBeInTheDocument();
  });

  it("says so plainly when there is no activity", () => {
    render(<ActivityTimeline events={[]} />);
    expect(screen.getByText("No activity yet.")).toBeInTheDocument();
  });

  it("labels actions an agent initiated, so a human can see what was done for them", () => {
    render(<ActivityTimeline events={[event({ origin: "AGENT" })]} />);
    expect(screen.getByText("via agent")).toBeInTheDocument();
  });

  it("labels the human's own decisions", () => {
    render(<ActivityTimeline events={[event({ origin: "HUMAN", kind: "approval.decided" })]} />);
    expect(screen.getByText("you")).toBeInTheDocument();
  });
});

describe("evidence expansion", () => {
  const withEvidence = event({ detail: { files_indexed: 312, path: "src/app/page.tsx" } });

  it("hides evidence until the step is expanded", () => {
    render(<ActivityTimeline events={[withEvidence]} />);
    const toggle = screen.getByRole("button", { name: "Scanning repository" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("312")).not.toBeVisible();
  });

  it("reveals counts and paths when expanded", async () => {
    render(<ActivityTimeline events={[withEvidence]} />);
    await userEvent.click(screen.getByRole("button", { name: "Scanning repository" }));
    expect(screen.getByText("312")).toBeVisible();
    expect(screen.getByText("src/app/page.tsx")).toBeVisible();
  });

  it("is expandable by keyboard", async () => {
    render(<ActivityTimeline events={[withEvidence]} />);
    await userEvent.tab();
    const toggle = screen.getByRole("button", { name: "Scanning repository" });
    expect(toggle).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  it("offers no toggle for a step with no evidence", () => {
    render(<ActivityTimeline events={[event()]} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("no chain-of-thought reaches the screen", () => {
  it("never renders reasoning-shaped evidence, even if it arrives", async () => {
    // 04_FRONTEND_SPEC.md §3. The API does not send these; this is the second
    // line of defence, so neither tier can regress alone.
    render(
      <ActivityTimeline
        events={[
          event({
            detail: {
              files_indexed: 5,
              reasoning: "First I considered the router, then...",
              chain_of_thought: "step 1: ...",
              internal_thoughts: "hmm",
            },
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Scanning repository" }));

    expect(screen.getByText("5")).toBeVisible();
    expect(screen.queryByText(/considered the router/)).not.toBeInTheDocument();
    expect(screen.queryByText(/step 1:/)).not.toBeInTheDocument();
    expect(screen.queryByText("hmm")).not.toBeInTheDocument();
  });

  it("classifies reasoning-shaped keys as unrenderable", () => {
    for (const key of [
      "reasoning",
      "chain_of_thought",
      "internal_thoughts",
      "model_rationale",
      "Scratchpad",
      "thinking_trace",
    ]) {
      expect(isRenderableEvidence(key), key).toBe(false);
    }
  });

  it("still allows genuine evidence keys", () => {
    for (const key of ["files_indexed", "path", "exit_code", "routes", "duration_ms"]) {
      expect(isRenderableEvidence(key), key).toBe(true);
    }
  });
});
