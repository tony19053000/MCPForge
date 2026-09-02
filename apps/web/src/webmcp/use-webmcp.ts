"use client";

import { useEffect, useState } from "react";
import type { ApiClient } from "@/lib/api/client";
import { env } from "@/lib/env";
import { createAdapter } from "@/webmcp/adapter";
import type { WebMCPAdapter } from "@/webmcp/adapter";
import { registerMCPForgeTools } from "@/webmcp/register";

/**
 * Mounts MCPForge's WebMCP tools for the current session — F7-01, F7-02.
 *
 * This is the only place `createAdapter` is called, and the only place the
 * development mock can be selected. `env.webmcpMock` is already false in a
 * production build (`lib/env.ts`), so the mock cannot reach a deployed page —
 * but reading it here, rather than passing a literal, is what makes that
 * guarantee reach the running product instead of stopping at a unit test.
 *
 * Registration is torn down by aborting the controller on unmount, because the
 * WebMCP draft has no `unregisterTool`.
 */

export interface WebMCPState {
  /** Which object the API was found on, or that it was not found. */
  surface: WebMCPAdapter["surface"];
  supported: boolean;
  /** True only for the labelled development mock. The UI must show this. */
  isMock: boolean;
  toolNames: readonly string[];
}

const UNMOUNTED: WebMCPState = {
  surface: "unsupported",
  supported: false,
  isMock: false,
  toolNames: [],
};

export function useWebMCP(client: ApiClient, sessionId: string | null): WebMCPState {
  const [state, setState] = useState<WebMCPState>(UNMOUNTED);

  useEffect(() => {
    if (!sessionId) return;

    const controller = new AbortController();
    let cancelled = false;

    void (async () => {
      const adapter = createAdapter({ useMock: env.webmcpMock });
      const result = await registerMCPForgeTools(
        adapter,
        client,
        sessionId,
        controller.signal,
      );
      if (cancelled) return;
      setState({
        surface: result.surface,
        supported: result.supported,
        isMock: result.isMock,
        toolNames: result.tools.map((t) => t.name),
      });
    })();

    return () => {
      cancelled = true;
      // Aborting is the teardown. Without it, tools registered by an unmounted
      // session would stay callable by an agent.
      controller.abort();
    };
  }, [client, sessionId]);

  // Derived rather than set in the effect: with no session there is nothing
  // registered, so reporting UNMOUNTED is a fact about the arguments, not state
  // to synchronise. Setting it in the effect would cascade a render for nothing.
  return sessionId ? state : UNMOUNTED;
}
