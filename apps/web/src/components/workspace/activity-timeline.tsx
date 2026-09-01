"use client";

import { Badge } from "@/components/ui/badge";
import type { EventDto } from "@/lib/api/types";

/**
 * Activity timeline — 04_FRONTEND_SPEC.md §3.
 *
 * Shows task-level summaries and verifiable detail: counts, paths, exit codes.
 * It never shows model reasoning, and the API never sends any.
 */

const STATUS_GLYPH: Record<string, string> = {
  "step.started": "◐",
  "step.completed": "✓",
  "step.failed": "✕",
  "approval.requested": "⏸",
  "approval.decided": "✓",
};

function toneFor(kind: string): "neutral" | "success" | "danger" | "pending" {
  if (kind === "step.failed") return "danger";
  if (kind === "approval.requested") return "pending";
  if (kind.endsWith(".completed") || kind === "approval.decided") return "success";
  return "neutral";
}

export function ActivityTimeline({ events }: { events: readonly EventDto[] }) {
  if (events.length === 0) {
    return <p className="text-sm text-subtle">No activity yet.</p>;
  }

  return (
    <ol aria-label="Activity" className="flex flex-col gap-2">
      {events.map((event) => (
        <li key={event.id} className="flex items-start gap-3 text-sm">
          <span aria-hidden="true" className="mt-0.5 w-4 text-center text-muted">
            {STATUS_GLYPH[event.kind] ?? "·"}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-text">{event.label}</span>
              {event.origin === "AGENT" ? (
                <Badge tone="accent" glyph="⚙">
                  via agent
                </Badge>
              ) : null}
              {event.origin === "HUMAN" ? <Badge tone="success">you</Badge> : null}
            </div>
            {Object.keys(event.detail).length > 0 ? (
              <dl className="mt-1 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-subtle">
                {Object.entries(event.detail).map(([key, value]) => (
                  <div key={key} className="flex gap-1">
                    <dt>{key}:</dt>
                    <dd className="text-muted">{String(value)}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </div>
          <span className="sr-only">{toneFor(event.kind)}</span>
        </li>
      ))}
    </ol>
  );
}
