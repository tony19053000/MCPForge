import { afterEach, describe, expect, it, vi } from "vitest";
import { MockModelContext, createAdapter, type ModelContextTool } from "@/webmcp/adapter";
import { readPublicEnv } from "@/lib/env";

function tool(name = "demo"): ModelContextTool {
  return {
    name,
    description: "a tool",
    inputSchema: { type: "object", properties: {} },
    execute: async () => ({ ok: true }),
  };
}

afterEach(() => {
  delete (document as unknown as { modelContext?: unknown }).modelContext;
  delete (navigator as unknown as { modelContext?: unknown }).modelContext;
  vi.unstubAllEnvs();
});

describe("WebMCP detection", () => {
  it("uses document.modelContext when the browser provides it", async () => {
    const registerTool = vi.fn(async () => {});
    (document as unknown as { modelContext: unknown }).modelContext = { registerTool };

    const adapter = createAdapter();
    expect(adapter.surface).toBe("document");
    expect(adapter.supported).toBe(true);
    expect(adapter.isMock).toBe(false);

    await adapter.register(tool(), new AbortController().signal);
    expect(registerTool).toHaveBeenCalledOnce();
  });

  it("falls back to navigator.modelContext when document has none", async () => {
    const registerTool = vi.fn(async () => {});
    (navigator as unknown as { modelContext: unknown }).modelContext = { registerTool };

    const adapter = createAdapter();
    expect(adapter.surface).toBe("navigator");
    expect(adapter.supported).toBe(true);

    await adapter.register(tool(), new AbortController().signal);
    expect(registerTool).toHaveBeenCalledOnce();
  });

  it("prefers document over navigator when both are present", () => {
    (document as unknown as { modelContext: unknown }).modelContext = {
      registerTool: async () => {},
    };
    (navigator as unknown as { modelContext: unknown }).modelContext = {
      registerTool: async () => {},
    };
    expect(createAdapter().surface).toBe("document");
  });

  it("ignores a modelContext that has no registerTool function", () => {
    (document as unknown as { modelContext: unknown }).modelContext = { registerTool: "nope" };
    expect(createAdapter().surface).toBe("unsupported");
  });

  it("degrades cleanly and honestly when WebMCP is absent", async () => {
    const adapter = createAdapter();
    expect(adapter.surface).toBe("unsupported");
    expect(adapter.supported).toBe(false);
    expect(adapter.isMock).toBe(false);
    // Registering must not throw: most browsers do not implement WebMCP yet and
    // MCPForge has to keep working for humans.
    await expect(adapter.register(tool(), new AbortController().signal)).resolves.toBeUndefined();
  });
});

describe("mock isolation", () => {
  it("is never selected automatically, even with no real surface present", () => {
    expect(createAdapter().isMock).toBe(false);
  });

  it("labels itself as a mock so the UI can never present it as real support", () => {
    const adapter = createAdapter({ useMock: true });
    expect(adapter.isMock).toBe(true);
    expect(adapter.surface).toBe("mock");
  });

  it("does not touch the real surface when the mock is in use", async () => {
    const registerTool = vi.fn(async () => {});
    (document as unknown as { modelContext: unknown }).modelContext = { registerTool };

    const mock = new MockModelContext();
    const adapter = createAdapter({ useMock: true, mock });
    await adapter.register(tool(), new AbortController().signal);

    expect(registerTool).not.toHaveBeenCalled();
    expect(mock.tools).toHaveLength(1);
  });

  it("cannot be enabled in a production build", () => {
    vi.stubEnv("NEXT_PUBLIC_WEBMCP_MOCK", "true");
    vi.stubEnv("NODE_ENV", "production");
    expect(readPublicEnv().webmcpMock).toBe(false);

    vi.stubEnv("NODE_ENV", "development");
    expect(readPublicEnv().webmcpMock).toBe(true);
  });
});

describe("teardown", () => {
  it("removes the tool from the mock when the signal aborts", async () => {
    const mock = new MockModelContext();
    const adapter = createAdapter({ useMock: true, mock });
    const controller = new AbortController();

    await adapter.register(tool("a"), controller.signal);
    await adapter.register(tool("b"), controller.signal);
    expect(mock.tools.map((t) => t.name)).toEqual(["a", "b"]);

    controller.abort();
    expect(mock.tools).toHaveLength(0);
  });

  it("tears down only the tools sharing the aborted signal", async () => {
    const mock = new MockModelContext();
    const adapter = createAdapter({ useMock: true, mock });
    const first = new AbortController();
    const second = new AbortController();

    await adapter.register(tool("a"), first.signal);
    await adapter.register(tool("b"), second.signal);

    first.abort();
    expect(mock.tools.map((t) => t.name)).toEqual(["b"]);
  });

  it("passes the signal through to a real browser surface, since there is no unregisterTool", async () => {
    const registerTool =
      vi.fn<(tool: ModelContextTool, options?: { signal?: AbortSignal }) => Promise<void>>(
        async () => {},
      );
    (document as unknown as { modelContext: unknown }).modelContext = { registerTool };
    const controller = new AbortController();

    await createAdapter().register(tool(), controller.signal);

    expect(registerTool.mock.calls[0]?.[1]).toEqual({ signal: controller.signal });
  });
});

describe("mock invocation", () => {
  it("executes a registered tool the way an agent would call it", async () => {
    const mock = new MockModelContext();
    const adapter = createAdapter({ useMock: true, mock });
    await adapter.register(
      { name: "echo", description: "echo", execute: async (input) => input },
      new AbortController().signal,
    );
    await expect(mock.call("echo", { x: 1 })).resolves.toEqual({ x: 1 });
  });

  it("rejects a call to a tool that is not registered", async () => {
    await expect(new MockModelContext().call("missing")).rejects.toThrow(/missing/);
  });
});
