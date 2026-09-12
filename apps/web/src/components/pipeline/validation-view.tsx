import { Badge } from "@/components/ui/badge";
import { validationPassed } from "@/components/pipeline/journey";
import type { ValidationCheckDto, ValidationDto } from "@/lib/api/types";

/**
 * The stored validation result and readiness score — ticket T5b,
 * `04_FRONTEND_SPEC.md` §9.
 *
 * Pass/fail is the pipeline's own (`outcome_of`, re-run by the read route). A
 * validation that could not run is not passed, a skipped check says why, and
 * the score is shown only when the server computed one from executed checks.
 */

const CHECK_BADGE = {
  passed: { tone: "success", glyph: "✓", text: "Passed" },
  failed: { tone: "danger", glyph: "✕", text: "Failed" },
  skipped: { tone: "neutral", glyph: "–", text: "Skipped" },
} as const;

export function ValidationView({ validation }: { validation: ValidationDto | null }) {
  const passed = validationPassed(validation);

  const blocking: string[] = [];
  if (validation === null) {
    blocking.push("No validation result is stored for this run.");
  } else {
    if (!validation.completed) {
      blocking.push(`Validation could not run: ${validation.reason ?? "no reason was recorded."}`);
    }
    for (const id of validation.failed_check_ids) blocking.push(`Check failed: ${id}`);
    for (const id of validation.unexecuted_tool_checks) blocking.push(`Required check did not run: ${id}`);
    if (validation.completed && !validation.passed && blocking.length === 0) {
      blocking.push(validation.summary ?? "The pipeline did not pass this validation.");
    }
  }

  return (
    <section aria-labelledby="validation-heading" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="validation-heading" className="text-sm font-medium text-text">
          Validation
        </h3>
        {passed ? (
          <Badge tone="success" glyph="✓">
            Passed
          </Badge>
        ) : (
          <Badge tone="danger" glyph="✕">
            Not passed
          </Badge>
        )}
      </div>

      {validation?.summary ? <p className="text-sm text-text">{validation.summary}</p> : null}

      {blocking.length > 0 ? (
        <div role="alert" className="rounded-control bg-danger-subtle p-3 text-sm text-text">
          <p className="font-medium">Blocking issues</p>
          <ul className="mt-1 list-disc pl-5">
            {blocking.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {validation && validation.checks.length > 0 ? (
        <table className="w-full text-left text-xs">
          <caption className="sr-only">Validation checks</caption>
          <thead className="text-subtle">
            <tr>
              <th scope="col">Check</th>
              <th scope="col">Result</th>
              <th scope="col">Evidence</th>
            </tr>
          </thead>
          <tbody className="align-top text-text">
            {validation.checks.map((c) => (
              <CheckRow key={c.check_id} check={c} />
            ))}
          </tbody>
        </table>
      ) : null}

      {validation ? <Readiness validation={validation} /> : null}

      {validation?.dependency_source ? (
        <p className="text-xs text-subtle">
          Dependencies: {validation.dependency_source}
          {validation.dependency_detail ? ` · ${validation.dependency_detail}` : ""}
        </p>
      ) : null}

      <p className="text-xs text-subtle">
        The validation read does not return the trust level this run executed under. The trust
        panel shows the server&apos;s current execution boundary.
      </p>
    </section>
  );
}

function CheckRow({ check }: { check: ValidationCheckDto }) {
  const b = CHECK_BADGE[check.status];
  const hasOutput = check.stdout_excerpt !== "" || check.stderr_excerpt !== "";
  return (
    <tr aria-label={`Check ${check.check_id}`}>
      <td className="py-1 pr-2">
        <span className="font-mono">{check.check_id}</span>
        <span className="block text-muted">{check.description}</span>
      </td>
      <td className="py-1 pr-2">
        <Badge tone={b.tone} glyph={b.glyph}>
          {b.text}
        </Badge>
      </td>
      <td className="py-1 text-muted">
        {check.status === "skipped" ? (
          <span>Not run: {check.skip_reason ?? "no reason was recorded."}</span>
        ) : (
          <span className="font-mono">
            exit {check.exit_code ?? "none"}
            {check.timed_out ? " · timed out" : ""}
            {check.duration_seconds !== null ? ` · ${check.duration_seconds.toFixed(1)}s` : ""}
          </span>
        )}
        {hasOutput ? (
          <details className="mt-1">
            <summary className="cursor-pointer">Output excerpt (redacted by the server)</summary>
            <pre className="mt-1 whitespace-pre-wrap font-mono text-subtle">
              {[check.stdout_excerpt, check.stderr_excerpt].filter(Boolean).join("\n")}
            </pre>
          </details>
        ) : null}
      </td>
    </tr>
  );
}

function Readiness({ validation }: { validation: ValidationDto }) {
  if (validation.score === null) {
    return (
      <p className="text-sm text-muted">
        No readiness score: validation produced no evidence to score.
      </p>
    );
  }
  const { total, max_total, components } = validation.score;
  return (
    <div aria-label="Agent readiness" role="group" className="flex flex-col gap-2">
      <p className="text-sm text-text">
        Agent readiness{" "}
        <span className="font-mono font-semibold">
          {total} / {max_total}
        </span>
      </p>
      <table className="w-full text-left text-xs">
        <caption className="sr-only">Readiness score components</caption>
        <thead className="text-subtle">
          <tr>
            <th scope="col">Component</th>
            <th scope="col">Points</th>
            <th scope="col">Checks</th>
            <th scope="col">Reason</th>
          </tr>
        </thead>
        <tbody className="text-text">
          {components.map((c) => (
            <tr key={c.component}>
              <td>{c.label}</td>
              <td className="font-mono">
                {c.points}/{c.weight}
              </td>
              <td className="font-mono">
                {c.checks_passed}/{c.checks_executed}
              </td>
              <td className="text-muted">{c.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
