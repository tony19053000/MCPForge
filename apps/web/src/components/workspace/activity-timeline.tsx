"use client";

import { useId, useState } from "react";
import { Badge } from "@/components/ui/badge";
import type { EventDto } from "@/lib/api/types";

/**
 * Activity timeline — 04_FRONTEND_SPEC.md §3.
 *
 * Steps show task-level summaries and expand to reveal evidence: counts, paths,
 * exit codes. They never show model reasoning, the API never sends any, and
 * `EVIDENCE_DENYLIST` makes that a rendering rule rather than only a convention.
 */

const STATUS_GLYPH: Record<string, string> = {
  "step.started": "◐",
  "step.completed": "✓",
  "step.failed": "✕",
  "approval.requested": "⏸",
  "approval.decided": "✓",
};

/**
 * Reasoning-shaped keys are never rendered, even if something upstream starts
 * sending them. Defence in depth behind the API, which does not send them.
 */
const EVIDENCE_DENYLIST = [
  "thought",
  "thoughts",
  "thinking",
  "reasoning",
  "chain_of_thought",
  "rationale",
  "scratchpad",
  "deliberation",
];

export function isRenderableEvidence(key: string): boolean {
  const lowered = key.toLowerCase();
  return !EVIDENCE_DENYLIST.some((banned) => lowered.includes(banned));
}

function Step({ event }: { event: EventDto }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();

  const evidence = Object.entries(event.detail).filter(([key]) => isRenderableEvidence(key));
  const hasEvidence = evidence.length > 0;

  return (
    <li className="flex items-start gap-3 text-sm">
      <span aria-hidden="true" className="mt-0.5 w-4 shrink-0 text-center text-muted">
        {STATUS_GLYPH[event.kind] ?? "·"}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          {hasEvidence ? (
            <button
              type="button"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
              aria-controls={panelId}
              className="rounded-control text-left text-text underline decoration-dotted underline-offset-4 hover:text-accent"
            >
              {event.label}
            </button>
          ) : (
            <span className="text-text">{event.label}</span>
          )}

          {event.origin === "AGENT" ? (
            <Badge tone="accent" glyph="⚙">
              via agent
            </Badge>
          ) : null}
          {event.origin === "HUMAN" ? <Badge tone="success">you</Badge> : null}
        </div>

        {hasEvidence ? (
          <dl
            id={panelId}
            hidden={!open}
            className="mt-1 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-subtle"
          >
            {evidence.map(([key, value]) => (
              <div key={key} className="flex gap-1">
                <dt>{key}:</dt>
                <dd className="text-muted">{String(value)}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </div>
    </li>
  );
}

export function ActivityTimeline({ events }: { events: readonly EventDto[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-subtle">No activity yet.</p>;
  }

  return (
    <ol aria-label="Activity" className="flex flex-col gap-2">
      {events.map((event) => (
        <Step key={event.id} event={event} />
      ))}
    </ol>
  );
}
