import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { renderHook } from "@testing-library/react";
import { ApiClient } from "@/lib/api/client";
import { WebMCPStatus } from "@/components/workspace/webmcp-status";
import { useWebMCP } from "@/webmcp/use-webmcp";

/**
 * The tools existing is not the same as the tools being reachable. This file
 * covers the mount: without it the whole WebMCP surface is dead code, which is
 * exactly what shipped before this test existed.
 */

const client = new ApiClient(async () => "token", "http://api");

afterEach(() => {
  delete (document as unknown as { modelContext?: unknown }).modelContext;
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

function stubBrowserWebMCP() {
  const registerTool = vi.fn(async () => {});
  (document as unknown as { modelContext: unknown }).modelContext = { registerTool };
  return registerTool;
}

describe("mounting", () => {
  it("registers every tool with the browser when a session exists", async () => {
    const registerTool = stubBrowserWebMCP();
    const { result } = renderHook(() => useWebMCP(client, "sess_1"));

    await waitFor(() => expect(result.current.supported).toBe(true));
    expect(registerTool).toHaveBeenCalledTimes(12);
    expect(result.current.surface).toBe("document");
    expect(result.current.toolNames).toContain("get_project_status");
  });

  it("registers nothing until there is a session", async () => {
    const registerTool = stubBrowserWebMCP();
    renderHook(() => useWebMCP(client, null));
    await new Promise((r) => setTimeout(r, 0));
    expect(registerTool).not.toHaveBeenCalled();
  });

  it("aborts registration on unmount, since there is no unregisterTool", async () => {
    const registerTool = vi.fn(
      async (_tool: unknown, options?: { signal?: AbortSignal }) => {
        expect(options?.signal).toBeDefined();
      },
    );
    (document as unknown as { modelContext: unknown }).modelContext = { registerTool };

    const { result, unmount } = renderHook(() => useWebMCP(client, "sess_1"));
    await waitFor(() => expect(result.current.supported).toBe(true));

    const signal = (registerTool.mock.calls[0]?.[1] as { signal: AbortSignal }).signal;
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
  });

  it("does not use the mock unless the environment asks for one", async () => {
    // The guard in lib/env.ts is only worth anything if the mount reads it.
    vi.stubEnv("NEXT_PUBLIC_WEBMCP_MOCK", "false");
    stubBrowserWebMCP();
    const { result } = renderHook(() => useWebMCP(client, "sess_1"));
    await waitFor(() => expect(result.current.supported).toBe(true));
    expect(result.current.isMock).toBe(false);
  });

  it("reports unsupported rather than silently mocking when WebMCP is absent", async () => {
    const { result } = renderHook(() => useWebMCP(client, "sess_1"));
    await new Promise((r) => setTimeout(r, 0));
    expect(result.current.supported).toBe(false);
    expect(result.current.isMock).toBe(false);
    expect(result.current.toolNames).toHaveLength(0);
  });
});

describe("what the developer is told", () => {
  it("labels a mock as MOCK and denies it is real support", () => {
    render(
      <WebMCPStatus
        state={{ surface: "mock", supported: true, isMock: true, toolNames: ["a"] }}
      />,
    );
    expect(screen.getByText("MOCK")).toBeVisible();
    expect(screen.getByText(/not real browser support/i)).toBeVisible();
  });

  it("names the surface it actually found", () => {
    render(
      <WebMCPStatus
        state={{ surface: "navigator", supported: true, isMock: false, toolNames: ["a", "b"] }}
      />,
    );
    expect(screen.getByText(/navigator\.modelContext/)).toBeVisible();
    expect(screen.queryByText("MOCK")).not.toBeInTheDocument();
  });

  it("says plainly when the browser has no WebMCP", () => {
    render(
      <WebMCPStatus
        state={{ surface: "unsupported", supported: false, isMock: false, toolNames: [] }}
      />,
    );
    expect(screen.getByText(/does not implement WebMCP/i)).toBeVisible();
  });

  it("states that an agent cannot approve anything", () => {
    render(
      <WebMCPStatus
        state={{ surface: "document", supported: true, isMock: false, toolNames: ["a"] }}
      />,
    );
    expect(screen.getByText(/cannot approve/i)).toBeVisible();
  });
});
