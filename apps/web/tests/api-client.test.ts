import { describe, expect, it } from "vitest";
import { parseSseBlock } from "@/lib/api/client";

describe("SSE parsing", () => {
  it("parses a text delta", () => {
    expect(parseSseBlock('event: delta\ndata: {"text":"hello"}')).toEqual({
      type: "delta",
      text: "hello",
    });
  });

  it("parses an activity step", () => {
    expect(
      parseSseBlock('event: activity\ndata: {"id":"e1","kind":"step.started","label":"Thinking"}'),
    ).toEqual({ type: "activity", id: "e1", kind: "step.started", label: "Thinking" });
  });

  it("parses an error with its real message", () => {
    expect(
      parseSseBlock('event: error\ndata: {"message":"429 rate limited","kind":"model_error"}'),
    ).toEqual({ type: "error", message: "429 rate limited", kind: "model_error" });
  });

  it("parses done", () => {
    expect(parseSseBlock('event: done\ndata: {"session_id":"s1"}')).toEqual({
      type: "done",
      sessionId: "s1",
    });
  });

  it("ignores an unknown event rather than guessing at it", () => {
    expect(parseSseBlock('event: reasoning\ndata: {"text":"internal"}')).toBeNull();
  });

  it("ignores malformed JSON instead of throwing mid-stream", () => {
    expect(parseSseBlock("event: delta\ndata: {not json")).toBeNull();
  });

  it("ignores an incomplete block", () => {
    expect(parseSseBlock("event: delta")).toBeNull();
    expect(parseSseBlock('data: {"text":"orphan"}')).toBeNull();
  });
});
