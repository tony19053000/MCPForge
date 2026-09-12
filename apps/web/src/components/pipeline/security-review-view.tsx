import { Badge, type Tone } from "@/components/ui/badge";
import { reviewPassed } from "@/components/pipeline/journey";
import type { FindingDto, SecurityReviewDto, Severity } from "@/lib/api/types";

/**
 * The stored security review — ticket T5b.
 *
 * The verdict shown is the deterministic gate's (`evaluate_gate`), never the
 * model's. The model reviewer's own view is shown as advisory, and any text it
 * wrote is labelled as its note, not as a check result. A review that did not
 * complete, or is missing, renders as not passed.
 */

const SEVERITY_TONE: Record<Severity, Tone> = {
  CRITICAL: "danger",
  HIGH: "danger",
  MEDIUM: "warning",
  LOW: "neutral",
  INFO: "neutral",
};

export function SecurityReviewView({ review }: { review: SecurityReviewDto | null }) {
  const passed = reviewPassed(review);

  return (
    <section aria-labelledby="security-review-heading" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="security-review-heading" className="text-sm font-medium text-text">
          Security review
        </h3>
        {passed ? (
          <Badge tone="success" glyph="✓">
            Passed the policy gate
          </Badge>
        ) : (
          <Badge tone="danger" glyph="✕">
            Not passed
          </Badge>
        )}
      </div>

      {review === null ? (
        <p className="text-sm text-muted">No security review is stored for this run.</p>
      ) : !review.completed ? (
        <p role="alert" className="rounded-control bg-danger-subtle p-3 text-sm text-text">
          The security review did not complete: {review.reason}
        </p>
      ) : (
        <>
          <p className="text-sm text-text">Gate verdict: {review.reason}</p>
          {review.agent_said_pass !== null ? (
            <p className="text-xs text-muted">
              The model reviewer said {review.agent_said_pass ? "pass" : "do not pass"}. That is
              advisory; the gate verdict above decides.
              {review.overridden ? " The gate overrode the model reviewer's view." : ""}
            </p>
          ) : null}
          {review.findings.length === 0 ? (
            <p className="text-sm text-muted">No findings were recorded.</p>
          ) : (
            <ul aria-label="Security findings" className="flex flex-col gap-2">
              {review.findings.map((f, i) => (
                <FindingRow key={`${f.rule}-${i}`} finding={f} />
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function FindingRow({ finding }: { finding: FindingDto }) {
  // `deterministic` is forced to false on every model finding by the gate
  // (`evaluate_gate`), so only the policy engine's findings carry it.
  const source = finding.deterministic ? "policy engine" : "model reviewer";
  const where = finding.evidence
    ? `${finding.evidence.path}${finding.evidence.line !== null ? `:${finding.evidence.line}` : ""}`
    : null;

  return (
    <li className="rounded-control border border-border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={SEVERITY_TONE[finding.severity]}>{finding.severity}</Badge>
        <span className="font-mono text-xs text-text">{finding.rule}</span>
        <span className="text-xs text-subtle">source: {source}</span>
        {where ? <span className="font-mono text-xs text-subtle">{where}</span> : null}
      </div>
      {finding.deterministic ? (
        <p className="mt-1 text-muted">{finding.summary}</p>
      ) : (
        <p className="mt-1 text-muted">
          <span className="text-subtle">Model reviewer&apos;s note (not a check result): </span>
          {finding.summary}
        </p>
      )}
    </li>
  );
}
