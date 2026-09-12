import { Badge } from "@/components/ui/badge";
import { safeHttpsUrl } from "@/components/pipeline/journey";
import type { PullRequestDto } from "@/lib/api/types";

/**
 * What the pull-request stage recorded — ticket T5b.
 *
 * Only what the writer stored: the URL, number and branch come from its
 * "pull request opened" record, and a failed attempt shows the server's reason.
 * Nothing here derives a URL or claims a PR the server has not recorded.
 */

const STATUS_BADGE = {
  OPENED: { tone: "success", glyph: "✓", text: "Opened" },
  CREATING: { tone: "accent", glyph: "◐", text: "Creating" },
  FAILED: { tone: "danger", glyph: "✕", text: "Failed" },
  AWAITING_APPROVAL: { tone: "pending", glyph: "⏸", text: "Awaiting approval" },
} as const;

export function PullRequestView({ pr }: { pr: PullRequestDto | null }) {
  if (pr === null) {
    return (
      <section aria-labelledby="pr-heading" className="flex flex-col gap-2">
        <h3 id="pr-heading" className="text-sm font-medium text-text">
          Pull request
        </h3>
        <p className="text-sm text-muted">No pull request is recorded for this run.</p>
      </section>
    );
  }

  const b = STATUS_BADGE[pr.status];
  const href = safeHttpsUrl(pr.url);

  return (
    <section aria-labelledby="pr-heading" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="pr-heading" className="text-sm font-medium text-text">
          Pull request
        </h3>
        <Badge tone={b.tone} glyph={b.glyph}>
          {b.text}
        </Badge>
      </div>

      {pr.status === "FAILED" ? (
        <p role="alert" className="rounded-control bg-danger-subtle p-3 text-sm text-text">
          Opening the pull request failed: {pr.failure ?? "no reason was recorded."}
        </p>
      ) : null}

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-subtle">Branch</dt>
        <dd className="font-mono text-text">{pr.branch ?? "not recorded"}</dd>
        {pr.number !== null ? (
          <>
            <dt className="text-subtle">Number</dt>
            <dd className="font-mono text-text">#{pr.number}</dd>
          </>
        ) : null}
        <dt className="text-subtle">URL</dt>
        <dd className="break-all text-text">
          {href ? (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-accent underline underline-offset-4"
            >
              {href}
            </a>
          ) : pr.url ? (
            // Shown, not linked: the server recorded something that is not an https URL.
            <span className="font-mono">{pr.url} (not a link: not an https URL)</span>
          ) : (
            "not recorded"
          )}
        </dd>
      </dl>

      {pr.status === "OPENED" ? (
        <p className="text-xs text-muted">
          MCPForge opened this pull request on its own branch. It does not merge it.
        </p>
      ) : null}
    </section>
  );
}
