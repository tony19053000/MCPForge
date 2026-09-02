/**
 * Registration — F7-02.
 *
 * One `AbortSignal` covers every tool, so a React effect's cleanup tears them
 * all down together. The WebMCP draft has no `unregisterTool`; the signal is
 * the teardown mechanism, which is why it is required here rather than optional.
 */

import type { ApiClient } from "@/lib/api/client";
import type { ModelContextTool, WebMCPAdapter } from "@/webmcp/adapter";
import { createTools } from "@/webmcp/tools";

export interface RegistrationResult {
  /** Tools actually registered. Empty when the browser has no WebMCP. */
  tools: readonly ModelContextTool[];
  supported: boolean;
  /** True only for the labelled development mock. The Trust Panel renders this. */
  isMock: boolean;
  surface: WebMCPAdapter["surface"];
}

export async function registerMCPForgeTools(
  adapter: WebMCPAdapter,
  client: ApiClient,
  sessionId: string,
  signal: AbortSignal,
): Promise<RegistrationResult> {
  const tools = createTools(client, sessionId);

  if (adapter.supported) {
    for (const tool of tools) {
      await adapter.register(tool, signal);
    }
  }

  return {
    // Report what was registered, not what was built: on an unsupported browser
    // these tools exist in memory but are reachable by nothing.
    tools: adapter.supported ? tools : [],
    supported: adapter.supported,
    isMock: adapter.isMock,
    surface: adapter.surface,
  };
}
