import type { MeasurementUnit } from "@/components/report/types";

/**
 * How a measured number is written on screen — F8-05.
 *
 * One function, used by the cell and by the change column, because two
 * formatters is how a rendered number stops matching the record it came from
 * and a test comparing them starts passing for the wrong reason.
 *
 * Seconds are never shown as `0.00`. A run that took 780 microseconds is a real
 * measurement, and rounding it to two decimals would put a zero on screen for
 * something that was not zero — the exact substitution this ticket exists to
 * prevent. Below 10ms the value is shown to two significant figures instead.
 */
export function formatValue(value: number, unit: MeasurementUnit): string {
  if (unit === "SECONDS") {
    return `${formatSeconds(value)} s`;
  }
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

/** A change, with an explicit sign so the direction is never inferred. */
export function formatDelta(delta: number, unit: MeasurementUnit): string {
  const sign = delta > 0 ? "+" : delta < 0 ? "−" : "±";
  return `${sign}${formatValue(Math.abs(delta), unit)}`;
}

function formatSeconds(value: number): string {
  const magnitude = Math.abs(value);
  if (magnitude === 0) {
    return "0.00";
  }
  return magnitude < 0.01 ? value.toPrecision(2) : value.toFixed(2);
}
