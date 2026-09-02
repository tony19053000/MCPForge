/**
 * MCPForge's own WebMCP adapter — ticket F7-01, 02_ARCHITECTURE.md §10.
 *
 * The single file that knows the browser API's shape, so spec drift is one
 * file's problem. The W3C Web Machine Learning CG draft puts the API on
 * `document.modelContext`; some early implementations expose
 * `navigator.modelContext`. Both are probed and the surface found is reported,
 * because the Trust Panel must state which one is in use rather than imply one.
 *
 * The draft has no `unregisterTool`. Teardown is the `AbortSignal` passed in
 * `registerTool`'s options, which is why every registration takes one.
 */

export interface ModelContextTool {
  name: string;
  title?: string;
  description: string;
  inputSchema?: Record<string, unknown>;
  execute: (input: Record<string, unknown>) => Promise<unknown>;
}

export interface ModelContextLike {
  registerTool(tool: ModelContextTool, options?: { signal?: AbortSignal }): Promise<void>;
}

/** Which object the API was found on, or that it was not found at all. */
export type AdapterSurface = "document" | "navigator" | "mock" | "unsupported";

export interface WebMCPAdapter {
  readonly surface: AdapterSurface;
  readonly supported: boolean;
  /** True only for the development mock. The Trust Panel renders this. */
  readonly isMock: boolean;
  register(tool: ModelContextTool, signal: AbortSignal): Promise<void>;
  /** Tools registered so far. The UI lists these; the mock also executes them. */
  readonly registered: readonly ModelContextTool[];
}

function probe(): { context: ModelContextLike; surface: "document" | "navigator" } | null {
  if (typeof document !== "undefined") {
    const found = (document as unknown as { modelContext?: ModelContextLike }).modelContext;
    if (typeof found?.registerTool === "function") return { context: found, surface: "document" };
  }
  if (typeof navigator !== "undefined") {
    const found = (navigator as unknown as { modelContext?: ModelContextLike }).modelContext;
    if (typeof found?.registerTool === "function") return { context: found, surface: "navigator" };
  }
  return null;
}

/**
 * A labelled stand-in for browsers without WebMCP.
 *
 * It is never selected automatically. It exists so the tools can be driven in
 * development and in Playwright, and it reports `isMock` so nothing can present
 * it as real browser support — 03_SECURITY_ACCESS.md and CLAUDE.md §6.6.
 */
export class MockModelContext implements ModelContextLike {
  readonly tools: ModelContextTool[] = [];

  async registerTool(tool: ModelContextTool, options?: { signal?: AbortSignal }): Promise<void> {
    this.tools.push(tool);
    options?.signal?.addEventListener("abort", () => {
      const index = this.tools.indexOf(tool);
      if (index >= 0) this.tools.splice(index, 1);
    });
  }

  /** Call a registered tool, as an agent would. */
  async call(name: string, input: Record<string, unknown> = {}): Promise<unknown> {
    const tool = this.tools.find((t) => t.name === name);
    if (!tool) throw new Error(`No tool named ${name} is registered`);
    return tool.execute(input);
  }
}

export interface AdapterOptions {
  /** Opt in to the mock. Never true in a production build — see lib/env.ts. */
  useMock?: boolean;
  /** Injectable for tests, so the real detection path is exercised. */
  mock?: MockModelContext;
}

export function createAdapter(options: AdapterOptions = {}): WebMCPAdapter {
  const registered: ModelContextTool[] = [];

  if (options.useMock) {
    const mock = options.mock ?? new MockModelContext();
    return {
      surface: "mock",
      supported: true,
      isMock: true,
      registered,
      async register(tool, signal) {
        registered.push(tool);
        await mock.registerTool(tool, { signal });
      },
    };
  }

  const found = probe();
  if (!found) {
    return {
      surface: "unsupported",
      supported: false,
      isMock: false,
      registered,
      async register() {
        // Not an error: most browsers do not implement WebMCP yet. MCPForge
        // keeps working for humans, which is the point.
      },
    };
  }

  return {
    surface: found.surface,
    supported: true,
    isMock: false,
    registered,
    async register(tool, signal) {
      registered.push(tool);
      await found.context.registerTool(tool, { signal });
    },
  };
}
