import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { ActivityTimeline } from "@/components/workspace/activity-timeline";
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

  it("shows verifiable detail like counts and paths", () => {
    render(
      <ActivityTimeline
        events={[event({ detail: { files_indexed: 312, path: "src/app/page.tsx" } })]}
      />,
    );
    expect(screen.getByText("312")).toBeInTheDocument();
    expect(screen.getByText("src/app/page.tsx")).toBeInTheDocument();
  });

  it("labels actions an agent initiated, so a human can see what was done for them", () => {
    render(<ActivityTimeline events={[event({ origin: "AGENT" })]} />);
    expect(screen.getByText("via agent")).toBeInTheDocument();
  });

  it("labels the human's own decisions", () => {
    render(<ActivityTimeline events={[event({ origin: "HUMAN", kind: "approval.decided" })]} />);
    expect(screen.getByText("you")).toBeInTheDocument();
  });

  it("says so plainly when there is no activity", () => {
    render(<ActivityTimeline events={[]} />);
    expect(screen.getByText("No activity yet.")).toBeInTheDocument();
  });
});
