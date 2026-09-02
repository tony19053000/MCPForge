"use client";

import { Badge } from "@/components/ui/badge";
import type { WebMCPState } from "@/webmcp/use-webmcp";

/**
 * States what MCPForge's own agent surface actually is — F7-01.
 *
 * The mock is labelled MOCK wherever it surfaces (CLAUDE.md §6.6). An
 * unsupported browser is reported as unsupported, not hidden: most browsers do
 * not implement WebMCP yet, and pretending otherwise would be the same lie in
 * the other direction.
 */
export function WebMCPStatus({ state }: { state: WebMCPState }) {
  return (
    <section aria-labelledby="webmcp-status-heading" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="webmcp-status-heading" className="text-sm font-medium text-text">
          Agent access
        </h2>
        {state.isMock ? (
          <Badge tone="warning" glyph="⚠">
            MOCK
          </Badge>
        ) : state.supported ? (
          <Badge tone="success" glyph="✓">
            live
          </Badge>
        ) : (
          <Badge tone="neutral">not available</Badge>
        )}
      </div>

      {state.isMock ? (
        <p className="text-xs text-muted">
          This is a development mock, not real browser support. Nothing here demonstrates a
          working WebMCP integration.
        </p>
      ) : state.supported ? (
        <p className="text-xs text-muted">
          {state.toolNames.length} tools registered on{" "}
          <span className="font-mono">{state.surface}.modelContext</span>. An agent can read this
          project and request changes — it cannot approve them.
        </p>
      ) : (
        <p className="text-xs text-muted">
          This browser does not implement WebMCP, so no tools are registered. MCPForge works
          normally for you.
        </p>
      )}
    </section>
  );
}
