"use client";

import { formatDelta } from "@/components/report/format";
import { MetricCell } from "@/components/report/metric-cell";
import type {
  BenchmarkReportDto,
  BenchmarkRunDto,
  RunSide,
} from "@/components/report/types";
import { Badge } from "@/components/ui/badge";

/**
 * The before/after demonstration — F8-05, `04_FRONTEND_SPEC.md` §9 and §8.
 *
 * The panel renders the report and nothing else. It computes no figure, holds
 * no default, and has no branch that substitutes a number for a missing one:
 * every value on screen comes out of a `MeasurementDto` that carries its own
 * run id and timestamp, and every metric the runs did not measure says so.
 *
 * The change column exists only where both sides were measured. A change
 * against an unmeasured side would be an extrapolation, so it is not offered —
 * the column says the comparison cannot be made and why. Pinned by
 * `states no change in the table where either side was not measured` in
 * `apps/web/tests/benchmark-comparison.test.tsx`, which queries the rendered
 * table rather than the report data: an earlier version asserted only against
 * the fixture, and rendering `±0` here left every test green.
 *
 * The tone is deliberately flat. Nothing here is styled as a success, because
 * "fewer errors" and "more approval points" are the developer's judgement to
 * make, not the report's — the same discipline as F8-03's trust panel, where a
 * green tick appears only for something genuinely verified.
 */
export function BenchmarkComparison({
  report,
}: {
  report: BenchmarkReportDto;
}) {
  const before = runFor(report, "BEFORE");
  const after = runFor(report, "AFTER");

  return (
    <section
      aria-labelledby="benchmark-heading"
      className="flex flex-col gap-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="benchmark-heading" className="text-sm font-medium text-text">
          Agent interaction — before and after
        </h2>
        <Badge tone="neutral" glyph="ⓘ">
          measured
        </Badge>
      </div>

      <table
        aria-label="Agent interaction measured before and after the transformation"
        className="w-full border-collapse text-left"
      >
        <thead>
          <tr className="border-b border-border text-xs uppercase tracking-wide text-subtle">
            <th scope="col" className="py-1 pr-3 font-normal">
              Metric
            </th>
            <th scope="col" className="py-1 pr-3 font-normal">
              Before
            </th>
            <th scope="col" className="py-1 pr-3 font-normal">
              After
            </th>
            <th scope="col" className="py-1 font-normal">
              Change
            </th>
          </tr>
        </thead>
        <tbody>
          {report.rows.map((row) => (
            <tr key={row.metric} className="border-b border-border/60">
              <th
                scope="row"
                className="py-1.5 pr-3 text-sm font-normal text-muted"
              >
                {row.label}
              </th>
              <td className="py-1.5 pr-3">
                <MetricCell cell={row.before} />
              </td>
              <td className="py-1.5 pr-3">
                <MetricCell cell={row.after} />
              </td>
              <td className="py-1.5">
                {row.delta === null ? (
                  <span
                    data-cell="delta_absent"
                    data-metric={row.metric}
                    title="This metric was not measured on both sides, so no change can be stated."
                    className="text-xs italic text-subtle"
                  >
                    No comparison
                  </span>
                ) : (
                  <span
                    data-cell="delta"
                    data-metric={row.metric}
                    className="font-mono text-sm text-muted"
                  >
                    {formatDelta(row.delta, row.unit)}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {before === undefined || after === undefined ? null : (
        <>
          <p className="text-xs text-subtle">{report.scenario_bound}</p>
          <IdenticalContainment report={report} />
          <dl className="grid grid-cols-1 gap-2 text-xs text-subtle sm:grid-cols-2">
            <RunFacts run={before} label="Before run" />
            <RunFacts run={after} label="After run" />
          </dl>
        </>
      )}
    </section>
  );
}

function runFor(
  report: BenchmarkReportDto,
  side: RunSide,
): BenchmarkRunDto | undefined {
  return report.runs.find((run) => run.side === side);
}

/**
 * The security statement of F8-05, rendered from the report's own profile.
 *
 * The backend refuses to build a report whose two runs were contained
 * differently, so this line is a restatement of an enforced invariant rather
 * than a claim the UI is making on its own.
 */
function IdenticalContainment({ report }: { report: BenchmarkReportDto }) {
  return (
    <p className="text-xs text-subtle">
      Both runs executed the same command inside the same kind of sandbox:{" "}
      <span className="font-mono">{report.sandbox.trust_level}</span>,{" "}
      {report.sandbox.allow_network ? "network allowed" : "no outbound network"}
      .
    </p>
  );
}

function RunFacts({ run, label }: { run: BenchmarkRunDto; label: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="uppercase tracking-wide">{label}</dt>
      <dd className="flex flex-col gap-0.5">
        <span className="truncate font-mono">run {run.run_id}</span>
        <span className="font-mono">started {run.started_at}</span>
        <span className="font-mono">
          exit {run.evidence.exit_code}
          {run.evidence.timed_out ? " · timed out" : ""}
        </span>
        <span className="font-mono">
          integration {run.integration ?? "unknown"}
        </span>
        {run.trace_complete ? null : (
          <span className="not-italic text-warning">{run.trace_note}</span>
        )}
      </dd>
    </div>
  );
}
