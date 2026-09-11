"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LOW_CONFIDENCE, type WorkflowView } from "@/components/pipeline/journey";
import { RiskChip } from "@/components/pipeline/risk-chip";

/**
 * Workflow cards — 04_FRONTEND_SPEC.md §7.
 *
 * Low-confidence workflows start unselected. Nothing is sent until the
 * developer presses the submit button, and what is sent is exactly the ticked
 * ids. The server checks each against the stored analysis.
 */
export function WorkflowSelector({
  workflows,
  busy,
  onSubmit,
}: {
  workflows: readonly WorkflowView[];
  busy: boolean;
  onSubmit: (workflowIds: string[]) => void;
}) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(
    () => new Set(workflows.filter((w) => w.confidence >= LOW_CONFIDENCE).map((w) => w.id)),
  );

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  if (workflows.length === 0) {
    return (
      <p className="text-sm text-muted">
        Analysis found no workflows in this repository, so there is nothing to select.
      </p>
    );
  }

  return (
    <form
      aria-label="Select workflows"
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(workflows.filter((w) => selected.has(w.id)).map((w) => w.id));
      }}
      className="flex flex-col gap-3"
    >
      <fieldset className="grid gap-3 sm:grid-cols-2">
        <legend className="mb-2 text-sm text-muted">
          Choose which workflows become WebMCP tools.
        </legend>
        {workflows.map((w) => {
          const inputId = `workflow-${w.id}`;
          return (
            <div key={w.id} className="rounded-control border border-border p-3">
              <div className="flex items-start gap-2">
                <input
                  id={inputId}
                  type="checkbox"
                  checked={selected.has(w.id)}
                  onChange={() => toggle(w.id)}
                  disabled={busy}
                  className="mt-1"
                />
                <div className="flex min-w-0 flex-col gap-1">
                  <label htmlFor={inputId} className="text-sm font-medium text-text">
                    {w.name}
                  </label>
                  <div className="flex flex-wrap gap-1">
                    <RiskChip risk={w.risk} />
                    {w.confidence < LOW_CONFIDENCE ? (
                      <Badge tone="warning" glyph="?">
                        low confidence
                      </Badge>
                    ) : null}
                  </div>
                  <p className="text-sm text-muted">{w.description}</p>
                  <details className="text-xs text-subtle">
                    <summary className="cursor-pointer">Evidence</summary>
                    <p className="mt-1 font-mono">calls {w.primaryFunction}</p>
                    <ul className="mt-1 font-mono">
                      {w.evidence.map((e) => (
                        <li key={`${e.path}:${e.symbol ?? ""}:${e.line ?? ""}`}>
                          {e.path}
                          {e.symbol ? ` · ${e.symbol}` : ""}
                          {e.line ? `:${e.line}` : ""}
                        </li>
                      ))}
                    </ul>
                  </details>
                </div>
              </div>
            </div>
          );
        })}
      </fieldset>
      <div>
        <Button
          type="submit"
          disabled={busy || selected.size === 0}
          disabledReason={selected.size === 0 ? "Select at least one workflow" : undefined}
        >
          Use {selected.size} selected workflow{selected.size === 1 ? "" : "s"}
        </Button>
      </div>
    </form>
  );
}
