"use client";

import type { ReactNode } from "react";

import { SecureExecutionRow } from "@/components/trust/secure-execution-row";
import { Badge } from "@/components/ui/badge";
import type { TrustStateDto } from "@/lib/api/types";
import type { WebMCPState } from "@/webmcp/use-webmcp";

/**
 * The trust panel — F8-03, `04_FRONTEND_SPEC.md` §8.
 *
 * Every row is state, not copy. The five server rows come from
 * `/api/sessions/{id}/trust`, which reads the stored project, the filtering
 * pipeline's own quarantine record, the live branch-writing constants and the
 * running execution provider. The sixth row is the browser's, because whether
 * `document.modelContext` exists is a fact this page can observe and a server
 * cannot: it is passed the same `WebMCPState` the real adapter produced in
 * `useWebMCP`, so a mock cannot reach this panel labelled as anything else.
 *
 * The panel renders no green tick anywhere except the verified execution
 * branch, which lives in `secure-execution-row.tsx` and is the only branch in
 * `apps/web/src` that tests for the attested trust level.
 */
export function TrustPanel({ trust, webmcp }: { trust: TrustStateDto; webmcp: WebMCPState }) {
  return (
    <section aria-labelledby="trust-panel-heading" className="flex flex-col gap-3">
      <h2 id="trust-panel-heading" className="text-sm font-medium text-text">
        Trust
      </h2>

      <dl className="flex flex-col gap-3">
        <Row label="Repository boundary">
          <RepositoryBoundary repository={trust.repository} />
        </Row>

        <Row label="Access mode">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm text-text">{trust.access_mode}</span>
            {trust.access_mode === "WRITE_PR" ? (
              <Badge tone="warning" glyph="⚠">
                can open a pull request
              </Badge>
            ) : null}
          </div>
        </Row>

        <Row label="Secret filtering">
          <SecretFiltering filtering={trust.secret_filtering} />
        </Row>

        <Row label="Secure execution">
          <SecureExecutionRow execution={trust.secure_execution} />
        </Row>

        <Row label="Branch protection">
          <p className="text-sm text-text">
            Writes restricted to{" "}
            <span className="font-mono">{trust.branch_protection.branch_prefix}*</span> branches
          </p>
          <p className="text-xs text-subtle">
            Never {trust.branch_protection.protected_names.join(", ")}, and never a force push.
          </p>
        </Row>

        <Row label="WebMCP adapter">
          <WebMCPAdapterRow state={webmcp} />
        </Row>
      </dl>
    </section>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-1 gap-1 sm:grid-cols-[10rem_1fr] sm:gap-3">
      <dt className="text-xs uppercase tracking-wide text-subtle">{label}</dt>
      <dd className="flex flex-col gap-1">{children}</dd>
    </div>
  );
}

function RepositoryBoundary({ repository }: { repository: TrustStateDto["repository"] }) {
  if (!repository.bound) {
    return (
      <p className="text-sm text-muted">
        No repository connected{repository.is_demo ? " — this is a demo project" : ""}. Nothing
        outside MCPForge is readable.
      </p>
    );
  }
  return (
    <p className="text-sm text-text">
      <span className="font-mono">{repository.repository_full_name}</span>
      {repository.base_branch ? (
        <>
          {" · branch: "}
          <span className="font-mono">{repository.base_branch}</span>
        </>
      ) : null}
    </p>
  );
}

/**
 * The count is the server's, and it is absent rather than zero until an
 * analysis has run — "0 files quarantined" would describe a scan that never
 * happened. The paths are shown on demand; contents are never sent and never
 * rendered (`03_SECURITY_ACCESS.md` §4).
 */
function SecretFiltering({ filtering }: { filtering: TrustStateDto["secret_filtering"] }) {
  if (!filtering.active) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-text">Inactive</span>
        <Badge tone="danger" glyph="⚠">
          no filtering rules are loaded
        </Badge>
      </div>
    );
  }

  if (!filtering.analyzed || filtering.quarantined_count === null) {
    return (
      <p className="text-sm text-text">
        Active · <span className="text-muted">nothing analyzed yet</span>
      </p>
    );
  }

  if (filtering.quarantined_count === 0) {
    return <p className="text-sm text-text">Active · 0 files quarantined</p>;
  }

  return (
    <details className="text-sm text-text">
      <summary className="cursor-pointer">
        Active · {filtering.quarantined_count}{" "}
        {filtering.quarantined_count === 1 ? "file" : "files"} quarantined
      </summary>
      <ul className="mt-1 flex flex-col gap-0.5">
        {filtering.quarantined_paths.map((path) => (
          <li key={path} className="font-mono text-xs text-muted">
            {path}
          </li>
        ))}
      </ul>
      <p className="mt-1 text-xs text-subtle">
        Paths only. These files were never opened, so their contents exist nowhere in MCPForge.
      </p>
    </details>
  );
}

/**
 * The adapter row states what the browser actually offers. The mock is labelled
 * as a mock wherever it surfaces (`CLAUDE.md` §6.6); an absent API is reported
 * as absent, because most browsers do not implement WebMCP yet.
 */
function WebMCPAdapterRow({ state }: { state: WebMCPState }) {
  if (state.isMock) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-warning">
          MOCK ADAPTER — not real browser WebMCP
        </span>
        <Badge tone="warning" glyph="⚠">
          mock
        </Badge>
      </div>
    );
  }

  if (!state.supported) {
    return <p className="text-sm text-text">not supported in this browser</p>;
  }

  return (
    <p className="text-sm text-text">
      <span className="font-mono">{state.surface}.modelContext</span> · supported
    </p>
  );
}
