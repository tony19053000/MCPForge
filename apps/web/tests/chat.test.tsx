import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Chat, type ChatTransport } from "@/components/workspace/chat";
import type { ChatEvent } from "@/lib/api/types";

function transportOf(events: ChatEvent[]): ChatTransport {
  return {
    async *chat() {
      for (const event of events) yield event;
    },
  };
}

const REPLY: ChatEvent[] = [
  { type: "turn", id: "t1", role: "user" },
  { type: "activity", id: "e1", kind: "step.started", label: "Thinking" },
  { type: "delta", text: "I found " },
  { type: "delta", text: "seven workflows" },
  { type: "activity", id: "e2", kind: "step.completed", label: "Thinking" },
  { type: "done", sessionId: "s1" },
];

async function send(text: string) {
  await userEvent.type(screen.getByLabelText("Message MCPForge"), text);
  await userEvent.click(screen.getByRole("button", { name: "Send" }));
}

describe("workspace chat", () => {
  it("shows the streamed reply", async () => {
    render(<Chat sessionId="s1" transport={transportOf(REPLY)} />);
    await send("analyze it");
    await waitFor(() => expect(screen.getByText(/seven workflows/)).toBeInTheDocument());
  });

  it("shows the user's own message immediately", async () => {
    render(<Chat sessionId="s1" transport={transportOf(REPLY)} />);
    await send("analyze it");
    expect(screen.getByText("analyze it")).toBeInTheDocument();
  });

  it("shows activity steps alongside the conversation", async () => {
    render(<Chat sessionId="s1" transport={transportOf(REPLY)} />);
    await send("hi");
    await waitFor(() => expect(screen.getByLabelText("Activity")).toBeInTheDocument());
  });

  it("shows the real error when the model fails", async () => {
    const failing = transportOf([
      { type: "error", message: "429 rate limited by the model API", kind: "model_error" },
    ]);
    render(<Chat sessionId="s1" transport={failing} />);
    await send("hi");
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("429 rate limited"),
    );
  });

  it("never shows a generic failure message", async () => {
    const failing = transportOf([
      { type: "error", message: "connection reset", kind: "model_error" },
    ]);
    render(<Chat sessionId="s1" transport={failing} />);
    await send("hi");
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
  });

  it("refuses to send an empty message", async () => {
    const chat = vi.fn();
    render(<Chat sessionId="s1" transport={{ chat } as unknown as ChatTransport} />);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("clears the composer after sending", async () => {
    render(<Chat sessionId="s1" transport={transportOf(REPLY)} />);
    await send("analyze it");
    await waitFor(() =>
      expect(screen.getByLabelText("Message MCPForge")).toHaveValue(""),
    );
  });

  it("renders existing turns on load", () => {
    render(
      <Chat
        sessionId="s1"
        transport={transportOf([])}
        initialTurns={[{ id: "t0", role: "user", text: "earlier message", origin: "HUMAN" }]}
      />,
    );
    expect(screen.getByText("earlier message")).toBeInTheDocument();
  });
});
