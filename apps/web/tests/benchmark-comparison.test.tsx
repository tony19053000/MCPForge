import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { BenchmarkComparison } from "@/components/report/benchmark-comparison";
import { formatValue } from "@/components/report/format";
import type {
  BenchmarkReportDto,
  MeasurementDto,
  MetricCellDto,
} from "@/components/report/types";

import full from "./fixtures/benchmark-report.json";
import partial from "./fixtures/benchmark-report-partial.json";

/**
 * The before/after demonstration — F8-05, `04_FRONTEND_SPEC.md` §9.
 *
 * The acceptance criteria are about honesty, so these tests are written against
 * the dishonest failure rather than the happy path: a number on screen that no
 * run produced, and a metric nobody measured shown as `0`.
 *
 * The central technique is that assertions compare rendered output **to the
 * report's own records**, never to other rendered text — with one boundary
 * worth naming, because getting it wrong is what round 2 of this ticket found.
 * The measured-cell assertion below compares against
 * `formatValue(record.value, record.unit)`, which puts the formatter on both
 * sides of the equality: replacing its body with `return "7";` left every test
 * here green. `report-format.test.ts` pins the formatter independently against
 * literal strings, which is what makes that comparison sound. Where a value is
 * *derived* rather than formatted — the Change column — the expected text is
 * built in the test from `row.delta` without calling `formatDelta`, or a forced
 * `+` sign inverting the headline comparison survives unnoticed.
 *
 * Phase 8's review record is the argument for comparing against records: a
 * quarantine count was asserted against a fixture that always produced exactly
 * that number, so a hardcoded constant passed, and
 * an attestation condition passed its whole suite because the fixtures modelled
 * the same wrong shape as the code. A test that reads the DOM and compares it
 * to the DOM proves only that the DOM is self-consistent.
 */

// Through `unknown` deliberately. A JSON import widens `[string, string][]` to
// `string[][]`, so a direct cast is rejected — and the fixtures are real
// `BenchmarkReport.model_dump_json()` output, which is the point of using them
// rather than hand-built objects that could drift from the backend's shape.
const report = full as unknown as BenchmarkReportDto;
const partialReport = partial as unknown as BenchmarkReportDto;

function cells(source: BenchmarkReportDto): MetricCellDto[] {
  return source.rows.flatMap((row) => [row.before, row.after]);
}

function measurements(source: BenchmarkReportDto): MeasurementDto[] {
  return cells(source).filter(
    (cell): cell is MeasurementDto => cell.state === "measured",
  );
}

describe("the before/after comparison", () => {
  it("every rendered number is a measurement in the report", () => {
    render(<BenchmarkComparison report={report} />);

    const rendered = screen.getAllByText(
      (_, element) => element?.getAttribute("data-cell") === "measured",
    );
    expect(rendered.length).toBe(measurements(report).length);
    expect(rendered.length).toBeGreaterThan(0);

    // Each cell is matched to the record it claims to come from, by run id and
    // metric and side — not to whatever else the table happens to show.
    for (const element of rendered) {
      const metric = element.getAttribute("data-metric");
      const side = element.getAttribute("data-side");
      const runId = element.getAttribute("data-run-id");
      const recordedAt = element.getAttribute("data-recorded-at");

      const backing = measurements(report).find(
        (measurement) =>
          measurement.metric === metric && measurement.side === side,
      );
      expect(
        backing,
        `rendered ${metric}/${side} has no measurement behind it`,
      ).toBeDefined();

      expect(runId).toBe(backing?.run_id);
      expect(recordedAt).toBe(backing?.recorded_at);
      expect(element.textContent).toBe(
        formatValue(backing!.value, backing!.unit),
      );

      // The run id is one the report actually records, not merely a non-empty
      // string: a cell could carry a plausible id belonging to no run.
      expect(report.runs.map((run) => run.run_id)).toContain(runId);
      expect(recordedAt).toBeTruthy();
    }
  });

  it("a metric that was not measured renders as absent, never as zero", () => {
    render(<BenchmarkComparison report={partialReport} />);

    const absent = screen.getAllByText(
      (_, element) => element?.getAttribute("data-cell") === "not_measured",
    );
    const expected = cells(partialReport).filter(
      (cell) => cell.state === "not_measured",
    );
    expect(absent.length).toBe(expected.length);
    expect(absent.length).toBeGreaterThan(0);

    for (const element of absent) {
      expect(element.textContent).toBe("Not measured");
      // The substitutions this ticket exists to prevent. `0 s` is included
      // because seconds format separately, and `formatSeconds` returns "0.00"
      // for a genuine zero — so a defaulted zero would be indistinguishable
      // from a measured one on screen.
      for (const substitute of ["0", "0.00", "0 s", "0.00 s", "—", "-", "n/a"]) {
        expect(element.textContent).not.toBe(substitute);
      }
      expect(element).not.toHaveAttribute("data-run-id");
      expect(element).not.toHaveAttribute("data-recorded-at");
    }
  });

  it("states no change in the table where either side was not measured", () => {
    const { container } = render(<BenchmarkComparison report={partialReport} />);

    const rowsWithoutBoth = partialReport.rows.filter(
      (row) => row.before.state !== "measured" || row.after.state !== "measured",
    );
    expect(rowsWithoutBoth.length).toBeGreaterThan(0);

    for (const row of rowsWithoutBoth) {
      // The data assertion is a precondition, not the criterion. An earlier
      // version of this test stopped here — it called `render` and then never
      // queried the DOM, so every assertion was against its own JSON fixture.
      // Deleting the `render` call changed nothing, and replacing the absent
      // branch with `formatDelta(0, ...)` rendered "±0" on every row of the
      // partial report while all four tests stayed green: a change computed
      // against a side nobody measured, which is precisely the "estimated,
      // extrapolated or illustrative" figure this ticket forbids.
      expect(
        row.delta,
        `${row.metric} carries a change across an unmeasured side`,
      ).toBeNull();

      const absent = container.querySelector(
        `[data-cell="delta_absent"][data-metric="${row.metric}"]`,
      );
      expect(
        absent,
        `${row.metric} has an unmeasured side but states no absence in the Change column`,
      ).not.toBeNull();
      expect(
        container.querySelector(`[data-cell="delta"][data-metric="${row.metric}"]`),
        `${row.metric} renders a change across an unmeasured side`,
      ).toBeNull();

      for (const substitute of ["0", "0.00", "±0", "±0.00", "±0 s", "±0.00 s", "—", "-", "n/a"]) {
        expect(absent?.textContent).not.toBe(substitute);
      }
    }

    // Non-vacuity: the assertions above are all "no element exists", which a
    // component rendering nothing at all would satisfy.
    const stated = container.querySelectorAll('[data-cell="delta_absent"]');
    expect(stated.length).toBe(rowsWithoutBoth.length);
    expect(stated.length).toBeGreaterThan(0);
  });

  it("states each change as the difference of the two records", () => {
    const { container } = render(<BenchmarkComparison report={report} />);

    const withDelta = report.rows.filter((row) => row.delta !== null);
    expect(withDelta.length).toBeGreaterThan(0);

    for (const row of withDelta) {
      const cell = container.querySelector(
        `[data-cell="delta"][data-metric="${row.metric}"]`,
      );
      expect(cell, `${row.metric} records a change but renders none`).not.toBeNull();

      // The expected text is built here from `row.delta`, deliberately without
      // calling `formatDelta`. Using the formatter would put the function under
      // test on both sides of the equality — the defect that let `return "7";`
      // pass every test in this file, and that let a forced `+` sign invert the
      // direction of the headline comparison unnoticed. `report-format.test.ts`
      // pins the formatter against literal strings; this pins the *value*.
      const delta = row.delta!;
      const sign = delta > 0 ? "+" : delta < 0 ? "−" : "±";
      const magnitude = Math.abs(delta);
      const written =
        row.unit === "SECONDS"
          ? `${magnitude === 0 ? "0.00" : magnitude < 0.01 ? magnitude.toPrecision(2) : magnitude.toFixed(2)} s`
          : Number.isInteger(magnitude)
            ? String(magnitude)
            : magnitude.toFixed(2);

      expect(
        cell?.textContent,
        `${row.metric} renders a change that is not after − before`,
      ).toBe(`${sign}${written}`);
    }

    const stated = container.querySelectorAll('[data-cell="delta"]');
    expect(stated.length).toBe(withDelta.length);
    expect(stated.length).toBeGreaterThan(0);
  });

  it("renders nothing a measurement did not supply", () => {
    // The counterweight to the first test, and the reason it is not vacuous: if
    // the component dropped every measured cell, the first test would still
    // pass on a count of zero. This asserts the fixture is rich enough to be
    // worth checking and that the table actually shows it.
    expect(measurements(report).length).toBeGreaterThanOrEqual(10);
    render(<BenchmarkComparison report={report} />);
    for (const measurement of measurements(report)) {
      const shown = screen.getAllByText(
        formatValue(measurement.value, measurement.unit),
      );
      expect(
        shown.length,
        `${measurement.metric}/${measurement.side} was measured but never rendered`,
      ).toBeGreaterThan(0);
    }
  });
});
