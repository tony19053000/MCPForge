import { describe, expect, it } from "vitest";

import { formatDelta, formatValue } from "@/components/report/format";

/**
 * How a measured number is written on screen — F8-05.
 *
 * Every expectation here is a **literal string**. That is the whole point of
 * the file: `benchmark-comparison.test.tsx` asserts
 * `element.textContent === formatValue(record.value, record.unit)`, which puts
 * the function under test on both sides of the equality. Replacing the body of
 * `formatValue` with `return "7";` — every metric on both sides rendering the
 * constant 7 — left all four of those tests green. The comparison there is
 * sound only if the formatter is pinned independently, which is what this file
 * does.
 *
 * The sub-10ms rule is asserted here too. It is the rule this ticket most
 * directly motivates — a real measurement shown as `0.00` is the substitution
 * F8-05 exists to prevent — and neither report fixture reaches the branch, so
 * deleting it left the suite green.
 */

describe("formatValue", () => {
  it("writes a count as the number itself", () => {
    expect(formatValue(0, "COUNT")).toBe("0");
    expect(formatValue(6, "COUNT")).toBe("6");
    expect(formatValue(12, "COUNT")).toBe("12");
  });

  it("writes a fractional count to two decimals", () => {
    expect(formatValue(1.5, "COUNT")).toBe("1.50");
    expect(formatValue(2.345, "COUNT")).toBe("2.35");
  });

  it("writes seconds to two decimals at and above 10ms", () => {
    expect(formatValue(0.01, "SECONDS")).toBe("0.01 s");
    expect(formatValue(0.019, "SECONDS")).toBe("0.02 s");
    expect(formatValue(1.5, "SECONDS")).toBe("1.50 s");
    expect(formatValue(12.345, "SECONDS")).toBe("12.35 s");
  });

  it("never shows a real measurement below 10ms as zero", () => {
    // The rule the ticket motivates. A run that took 780 microseconds is a
    // real measurement; rounding it to two decimals would put "0.00 s" on
    // screen for something that was not zero, and a defaulted zero would then
    // be indistinguishable from a measured one.
    for (const value of [0.00078, 0.0001, 0.009, 0.0099]) {
      const written = formatValue(value, "SECONDS");
      expect(written, `${value}s was rounded away`).not.toBe("0.00 s");
      expect(written).not.toBe("0 s");
      expect(Number.parseFloat(written)).toBeGreaterThan(0);
    }
    expect(formatValue(0.00078, "SECONDS")).toBe("0.00078 s");
  });

  it("writes a genuine zero as zero", () => {
    // The counterweight: the rule above must not turn a measured zero into
    // something else. Zero seconds is a real reading and says so.
    expect(formatValue(0, "SECONDS")).toBe("0.00 s");
  });
});

describe("formatDelta", () => {
  it("carries an explicit sign so the direction is never inferred", () => {
    expect(formatDelta(3, "COUNT")).toBe("+3");
    expect(formatDelta(-3, "COUNT")).toBe("−3");
    expect(formatDelta(0, "COUNT")).toBe("±0");
  });

  it("uses the minus sign, not the hyphen", () => {
    // U+2212, as rendered. Asserted because a hyphen would also look right and
    // the two are not interchangeable in the table's monospace column.
    expect(formatDelta(-3, "COUNT")).toBe("−3");
    expect(formatDelta(-3, "COUNT")).not.toBe("-3");
  });

  it("formats the magnitude the same way a value is formatted", () => {
    expect(formatDelta(-0.5, "SECONDS")).toBe("−0.50 s");
    expect(formatDelta(1.25, "SECONDS")).toBe("+1.25 s");
  });
});
