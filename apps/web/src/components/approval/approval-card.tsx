"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ApprovalDto } from "@/lib/api/types";

/**
 * Approval card — 04_FRONTEND_SPEC.md §4.
 *
 * The card states exactly what approving permits, shows which artifact version
 * it covers, and cannot be dismissed by clicking away. Its state comes from the
 * server: nothing renders as approved until the store confirms it.
 */

const GATE_MEANING: Record<ApprovalDto["gate"], string> = {
  TOOL_PLAN: "Approving lets MCPForge generate code for these tools. No repository changes yet.",
  PATCH: "Approving accepts this exact diff. It still will not be pushed anywhere yet.",
  PULL_REQUEST:
    "Approving opens a pull request on an mcpforge/* branch. Your default branch is untouched.",
  ACCESS_ELEVATION:
    "Approving lets MCPForge create a branch and a pull request on this repository.",
};

/**
 * Gates whose consequences reach outside MCPForge. 04_FRONTEND_SPEC.md §4
 * requires explicit re-confirmed intent for these, not a single click.
 */
const REQUIRES_TYPED_CONFIRMATION: ReadonlySet<ApprovalDto["gate"]> = new Set([
  "PULL_REQUEST",
  "ACCESS_ELEVATION",
]);

const CONFIRM_WORD = "approve";

export function ApprovalCard({
  approval,
  onDecide,
  onModify,
  /** Hash of the artifact currently on screen. If it differs, this card is stale. */
  currentArtifactHash,
}: {
  approval: ApprovalDto;
  onDecide: (decision: "APPROVED" | "REJECTED") => Promise<void>;
  /** Ask for changes instead of accepting or refusing outright. */
  onModify?: () => void;
  currentArtifactHash?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [typed, setTyped] = useState("");

  const isStale =
    currentArtifactHash !== undefined && currentArtifactHash !== approval.artifact_hash;
  const isDecided = approval.status !== "PENDING";
  const needsTyped = REQUIRES_TYPED_CONFIRMATION.has(approval.gate);
  const confirmed = !needsTyped || typed.trim().toLowerCase() === CONFIRM_WORD;

  async function decide(decision: "APPROVED" | "REJECTED") {
    setBusy(true);
    setError(null);
    try {
      await onDecide(decision);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      aria-labelledby={`approval-${approval.id}`}
      // Announced to assistive technology when it appears and when it resolves,
      // so a decision request is never silent for a screen-reader user.
      role="region"
      aria-live="polite"
      className="rounded-card border-2 border-pending bg-surface p-5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="pending" glyph="⏸">
          {isDecided ? approval.status : "Awaiting your decision"}
        </Badge>
        <span className="font-mono text-xs text-subtle">{approval.gate}</span>
      </div>

      <h2 id={`approval-${approval.id}`} className="mt-3 text-base font-semibold text-text">
        {approval.summary}
      </h2>
      <p className="mt-2 text-sm leading-relaxed text-muted">{GATE_MEANING[approval.gate]}</p>

      <p className="mt-3 font-mono text-xs text-subtle">
        version {approval.artifact_hash.slice(0, 12)}
      </p>

      {isStale ? (
        <p role="alert" className="mt-3 rounded-control bg-warning-subtle p-3 text-sm text-text">
          This has changed since the decision was requested. Approving is disabled — request a
          fresh approval for the current version.
        </p>
      ) : null}

      {error ? (
        <p role="alert" className="mt-3 rounded-control bg-danger-subtle p-3 text-sm text-text">
          {error}
        </p>
      ) : null}

      {isDecided ? (
        <p className="mt-4 text-sm text-muted">
          {approval.status === "APPROVED" ? "Approved" : "Rejected"}
          {approval.decided_at ? ` on ${new Date(approval.decided_at).toLocaleString()}` : ""}
        </p>
      ) : (
        <>
          {needsTyped && !isStale ? (
            <div className="mt-4">
              <label htmlFor={`confirm-${approval.id}`} className="text-sm text-text">
                This acts outside MCPForge. Type{" "}
                <span className="font-mono font-semibold">{CONFIRM_WORD}</span> to confirm.
              </label>
              <input
                id={`confirm-${approval.id}`}
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                autoComplete="off"
                className="mt-2 w-full rounded-control border border-border-strong bg-surface px-3 py-2 font-mono text-sm text-text"
              />
            </div>
          ) : null}

          <div className="mt-5 flex flex-wrap gap-2">
            <Button
              onClick={() => decide("APPROVED")}
              disabled={busy || isStale || !confirmed}
              disabledReason={
                isStale
                  ? "The artifact changed since this was requested"
                  : !confirmed
                    ? `Type ${CONFIRM_WORD} to confirm this action`
                    : undefined
              }
            >
              Approve
            </Button>
            {onModify ? (
              <Button variant="secondary" onClick={onModify} disabled={busy}>
                Modify
              </Button>
            ) : null}
            <Button variant="secondary" onClick={() => decide("REJECTED")} disabled={busy}>
              Reject
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
