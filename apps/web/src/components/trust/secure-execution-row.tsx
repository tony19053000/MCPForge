"use client";

import { Badge } from "@/components/ui/badge";
import type { SecureExecutionDto, TrustLevel } from "@/lib/api/types";

/**
 * The execution row of the trust panel — F8-03, `04_FRONTEND_SPEC.md` §8.
 *
 * **This file holds the product's only verified-execution branch.** The
 * comparison below is the single place in `apps/web/src` that tests for the
 * attested trust level, and the verified wording exists nowhere else. That is
 * not a convention: `tests/trust-verified-branch.test.ts` parses this tree,
 * finds every occurrence of the phrase and every comparison against
 * `HARDWARE_ATTESTED`, and fails if there is more than one of either or if the
 * phrase is not inside a branch guarded by that comparison.
 *
 * It mirrors the backend rule from `F8-01`, where exactly one function may
 * produce the attested level and exactly one may construct evidence.
 *
 * The trust level arrives from `/api/sessions/{id}/trust` and is read straight
 * from the running execution provider. There is no client-side default, no
 * environment flag and no prop that can raise it: a caller that renders this
 * component with no server state has no attested value to pass.
 */

/**
 * Human labels for the levels that are *not* attested. Keyed by the enum, so a
 * future member without a label renders as its raw value rather than borrowing
 * the wrong one.
 */
const UNATTESTED_LABELS: Partial<Record<TrustLevel, string>> = {
  DEVELOPMENT_ISOLATION: "Development Isolation",
};

export function SecureExecutionRow({ execution }: { execution: SecureExecutionDto }) {
  if (execution.trust_level === "HARDWARE_ATTESTED") {
    // The only verified branch. Reaching it requires the server to have read an
    // attested level off a real provider, which requires verified attestation
    // evidence (`execution/attestation.py`). Nothing in MCPForge produces one
    // today — `F8-02` is blocked — so this branch is unreachable in the running
    // product rather than merely unused.
    return (
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm text-text">Hardware-backed Confidential Execution Verified</span>
          <Badge tone="success" glyph="✓">
            attested
          </Badge>
        </div>
        {execution.evidence ? (
          <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-muted">
            <dt>image</dt>
            <dd className="truncate font-mono">{execution.evidence.image_digest}</dd>
            <dt>hardware</dt>
            <dd className="font-mono">{execution.evidence.hardware_model}</dd>
            <dt>workload</dt>
            <dd className="truncate font-mono">
              {execution.evidence.workload_service_account}
            </dd>
            <dt>verified</dt>
            <dd className="font-mono">{execution.evidence.verified_at}</dd>
          </dl>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-text">
          {UNATTESTED_LABELS[execution.trust_level] ?? execution.trust_level}
        </span>
        {/* Neutral, never a tick. Development isolation is real isolation and
            it is not attestation, so it is stated informationally. */}
        <Badge tone="neutral" glyph="ⓘ">
          unverified
        </Badge>
      </div>
      <p className="text-xs text-muted">Not hardware-attested</p>
      <p className="text-xs text-subtle">{execution.detail}</p>
      <p className="text-xs text-subtle">
        {execution.provider_running
          ? `provider: ${execution.configured_executor}`
          : "provider: none running"}
      </p>
    </div>
  );
}
