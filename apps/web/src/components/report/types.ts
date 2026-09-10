/**
 * The before/after benchmark, as it arrives from the backend — F8-05.
 *
 * These mirror `services/api/src/mcpforge/orchestration/benchmark.py`. The
 * shape that matters is `MetricCellDto`: a discriminated union where the
 * not-measured member has **no `value` field at all**. A metric that was not
 * measured therefore has no number to render, by construction rather than by
 * convention, which is what "absent rather than defaulted" means here.
 *
 * **Not wired to a route yet.** F8-05's scope is the measurement and its
 * rendering; no endpoint serves a `BenchmarkReportDto` today, so these types
 * describe the JSON `BenchmarkReport.model_dump_json()` produces and the
 * component is driven by a caller that has one. Nothing here should be read as
 * a claim that the workspace fetches a benchmark.
 */

export type RunSide = "BEFORE" | "AFTER";

export type MeasurementUnit = "COUNT" | "SECONDS";

export type InteractionMetric =
  | "INTERACTION_STEPS"
  | "TASKS_ATTEMPTED"
  | "TASKS_COMPLETED"
  | "ERRORS"
  | "RETRIES"
  | "APPROVAL_POINTS"
  | "ELAPSED_SECONDS";

export type IntegrationPresence = "present" | "absent";

/** `orchestration/scoring.py`'s `CheckEvidence`, shared with F8-04. */
export interface CheckEvidenceDto {
  check_id: string;
  argv: string[];
  exit_code: number;
  timed_out: boolean;
  duration_seconds: number;
  stdout_excerpt: string;
  stderr_excerpt: string;
  output_truncated: boolean;
}

export interface MeasurementDto {
  state: "measured";
  metric: InteractionMetric;
  side: RunSide;
  value: number;
  unit: MeasurementUnit;
  /** The run this number was counted from. Required; there is no default. */
  run_id: string;
  /** ISO-8601, when the measurement was recorded. Required. */
  recorded_at: string;
  derivation: string;
  evidence: CheckEvidenceDto;
}

export interface AbsenceDto {
  state: "not_measured";
  metric: InteractionMetric;
  side: RunSide;
  reason: string;
}

export type MetricCellDto = MeasurementDto | AbsenceDto;

export interface MetricRowDto {
  metric: InteractionMetric;
  label: string;
  unit: MeasurementUnit;
  before: MetricCellDto;
  after: MetricCellDto;
  /** `after - before`, and only when both sides were measured. */
  delta: number | null;
}

export interface SandboxProfileDto {
  trust_level: string;
  allow_network: boolean;
  argv: string[];
  cwd: string | null;
  timeout_seconds: number;
  env: [string, string][];
}

export interface BenchmarkRunDto {
  run_id: string;
  side: RunSide;
  workspace_id: string;
  started_at: string;
  completed_at: string;
  sandbox: SandboxProfileDto;
  evidence: CheckEvidenceDto;
  trace_complete: boolean;
  trace_note: string;
  integration: IntegrationPresence | null;
}

export interface BenchmarkReportDto {
  benchmark_id: string;
  sandbox: SandboxProfileDto;
  runs: BenchmarkRunDto[];
  rows: MetricRowDto[];
  scenario_bound: string;
}
