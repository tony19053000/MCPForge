import { formatValue } from "@/components/report/format";
import type { MetricCellDto } from "@/components/report/types";

/**
 * One cell of the before/after table — F8-05.
 *
 * Two branches and no third. A measured cell renders the number **from the
 * record** and carries the record's run id and timestamp as data attributes, so
 * a test can check the displayed figure against its backing measurement rather
 * than against other rendered text. An unmeasured cell renders the words "Not
 * measured" and its reason; it has no number to render, because `AbsenceDto`
 * carries none.
 *
 * There is no fallback that turns a missing measurement into `0`, and there is
 * nowhere for one to hide: this is the only component that renders a metric
 * value.
 *
 * Pinned by `apps/web/tests/benchmark-comparison.test.tsx`:
 * `every rendered number is a measurement in the report` and
 * `a metric that was not measured renders as absent, never as zero`. Both were
 * verified able to fail, by rendering `0` for an absence, by drifting a
 * rendered number from its record, and by carrying a run id belonging to no
 * run — a citation is worth nothing if the test it names cannot go red.
 */
export function MetricCell({ cell }: { cell: MetricCellDto }) {
  if (cell.state === "measured") {
    return (
      <span
        data-cell="measured"
        data-metric={cell.metric}
        data-side={cell.side}
        data-run-id={cell.run_id}
        data-recorded-at={cell.recorded_at}
        title={`${cell.derivation} Run ${cell.run_id}, recorded ${cell.recorded_at}.`}
        className="font-mono text-sm text-text"
      >
        {formatValue(cell.value, cell.unit)}
      </span>
    );
  }

  return (
    <span
      data-cell="not_measured"
      data-metric={cell.metric}
      data-side={cell.side}
      title={cell.reason}
      className="text-xs italic text-subtle"
    >
      Not measured
    </span>
  );
}
