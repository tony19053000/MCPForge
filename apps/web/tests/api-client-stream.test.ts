/**
 * ApiClient.chat() reader and abort path.
 *
 * parseSseBlock has its own tests; this covers the fetch, the chunked reader
 * loop and cancellation, which the review found untested.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "@/lib/api/client";
import type { ChatEvent } from "@/lib/api/types";

function streamOf(
  chunks: string[],
  options: { onCancel?: () => void; keepOpen?: boolean } = {},
): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      // A real SSE response stays open until the server ends it. keepOpen
      // models that, which is the only case where cancellation is meaningful.
      if (!options.keepOpen) controller.close();
    },
    cancel() {
      options.onCancel?.();
    },
  });
  return new Response(body, { status: 200 });
}

function clientWith(response: Response | (() => Promise<Response>)): ApiClient {
  const fetchMock = vi.fn(async () =>
    typeof response === "function" ? await response() : response,
  );
  vi.stubGlobal("fetch", fetchMock);
  return new ApiClient(async () => "test-token", "http://api.test");
}

async function collect(gen: AsyncGenerator<ChatEvent>): Promise<ChatEvent[]> {
  const out: ChatEvent[] = [];
  for await (const event of gen) out.push(event);
  return out;
}

afterEach(() => vi.unstubAllGlobals());

describe("ApiClient.chat", () => {
  it("yields events parsed from the stream", async () => {
    const client = clientWith(
      streamOf([
        'event: turn\ndata: {"id":"t1","role":"user"}\n\n',
        'event: delta\ndata: {"text":"hello "}\n\n',
        'event: delta\ndata: {"text":"world"}\n\n',
        'event: done\ndata: {"session_id":"s1"}\n\n',
      ]),
    );
    const events = await collect(client.chat("s1", "hi"));
    expect(events.map((e) => e.type)).toEqual(["turn", "delta", "delta", "done"]);
  });

  it("reassembles an event split across network chunks", async () => {
    const client = clientWith(
      streamOf(['event: delta\ndata: {"te', 'xt":"split"}\n\n']),
    );
    const events = await collect(client.chat("s1", "hi"));
    expect(events).toEqual([{ type: "delta", text: "split" }]);
  });

  it("handles several events arriving in one chunk", async () => {
    const client = clientWith(
      streamOf(['event: delta\ndata: {"text":"a"}\n\nevent: delta\ndata: {"text":"b"}\n\n']),
    );
    const events = await collect(client.chat("s1", "hi"));
    expect(events).toHaveLength(2);
  });

  it("sends the bearer token", async () => {
    const client = clientWith(streamOf(['event: done\ndata: {"session_id":"s1"}\n\n']));
    await collect(client.chat("s1", "hi"));
    const [, init] = vi.mocked(fetch).mock.calls[0]!;
    expect((init?.headers as Record<string, string>).Authorization).toBe("Bearer test-token");
  });

  it("surfaces the real error body on failure", async () => {
    const client = clientWith(
      async () => new Response("Gemini is not configured", { status: 503 }),
    );
    await expect(collect(client.chat("s1", "hi"))).rejects.toThrow(/not configured/);
    await expect(collect(client.chat("s1", "hi"))).rejects.toBeInstanceOf(ApiError);
  });

  it("releases the response body when the consumer stops early", async () => {
    // Without this, the server keeps streaming into a reader nobody is draining.
    const onCancel = vi.fn();
    const client = clientWith(
      streamOf(['event: delta\ndata: {"text":"a"}\n\n'], { onCancel, keepOpen: true }),
    );
    const gen = client.chat("s1", "hi");
    await gen.next();
    await gen.return(undefined as never);
    expect(onCancel).toHaveBeenCalled();
  });

  it("passes the abort signal to fetch", async () => {
    const client = clientWith(streamOf(['event: done\ndata: {"session_id":"s1"}\n\n']));
    const controller = new AbortController();
    await collect(client.chat("s1", "hi", controller.signal));
    const [, init] = vi.mocked(fetch).mock.calls[0]!;
    expect(init?.signal).toBe(controller.signal);
  });

  it("drops an unknown event rather than surfacing it", async () => {
    const client = clientWith(
      streamOf([
        'event: reasoning\ndata: {"text":"internal"}\n\n',
        'event: done\ndata: {"session_id":"s1"}\n\n',
      ]),
    );
    const events = await collect(client.chat("s1", "hi"));
    expect(events.map((e) => e.type)).toEqual(["done"]);
  });
});
