/**
 * The journey's pure logic — ticket T5a.
 *
 * Nothing here holds state. Every function reads a server payload and says
 * what it means; the backend run state is the only source of truth for whether
 * a stage is done. A payload that does not have the shape the backend models
 * define is refused with `MalformedPayloadError`, never filled with defaults.
 */

import { ApiError } from "@/lib/api/client";
import type {
  AgentArtifactDto,
  FindingDto,
  PatchDto,
  PatchFileDto,
  PullRequestDto,
  RunState,
  ScoreComponentDto,
  SecurityReviewDto,
  ValidationCheckDto,
  ValidationDto,
} from "@/lib/api/types";

/** `RunState`, in the order of `services/api/src/mcpforge/models/core.py`. */
export const RUN_STATE_ORDER: readonly RunState[] = [
  "PROJECT_CREATED",
  "REPOSITORY_CONNECTED",
  "ANALYSIS_PENDING",
  "ANALYSIS_RUNNING",
  "ANALYSIS_COMPLETE",
  "WORKFLOW_SELECTION_PENDING",
  "WORKFLOWS_SELECTED",
  "TOOL_PLAN_RUNNING",
  "TOOL_PLAN_READY",
  "TOOL_PLAN_APPROVAL_PENDING",
  "TOOL_PLAN_APPROVED",
  "GENERATION_RUNNING",
  "PATCH_READY",
  "SECURITY_REVIEW_RUNNING",
  "SECURITY_REVIEW_FAILED",
  "SECURITY_REVIEW_PASSED",
  "PATCH_APPROVAL_PENDING",
  "PATCH_APPROVED",
  "VALIDATION_RUNNING",
  "VALIDATION_FAILED",
  "VALIDATION_PASSED",
  "PR_APPROVAL_PENDING",
  "PR_APPROVED",
  "PR_CREATING",
  "PR_CREATED",
  "COMPLETE",
];

export function atLeast(state: RunState, milestone: RunState): boolean {
  return RUN_STATE_ORDER.indexOf(state) >= RUN_STATE_ORDER.indexOf(milestone);
}

export type StageStatus = "done" | "running" | "current" | "failed" | "not_yet";

export interface StageDef {
  id: string;
  label: string;
  /** The first run state that means this stage has finished. */
  doneAt: RunState;
  /** Run states that mean the server entered this stage and has not left it. */
  runningAt?: readonly RunState[];
  /** The run state that records this stage's own failed verdict. */
  failedAt?: RunState;
}

export const STAGES: readonly StageDef[] = [
  { id: "connect", label: "Connect repository", doneAt: "ANALYSIS_PENDING" },
  { id: "analyze", label: "Analyze", doneAt: "ANALYSIS_COMPLETE", runningAt: ["ANALYSIS_RUNNING"] },
  { id: "workflows", label: "Select workflows", doneAt: "WORKFLOWS_SELECTED" },
  { id: "plan", label: "Design tool plan", doneAt: "TOOL_PLAN_READY", runningAt: ["TOOL_PLAN_RUNNING"] },
  { id: "approve", label: "Approve tool plan", doneAt: "TOOL_PLAN_APPROVED" },
  { id: "generate", label: "Generate code", doneAt: "PATCH_READY", runningAt: ["GENERATION_RUNNING"] },
  {
    id: "review",
    label: "Security review",
    doneAt: "SECURITY_REVIEW_PASSED",
    runningAt: ["SECURITY_REVIEW_RUNNING"],
    failedAt: "SECURITY_REVIEW_FAILED",
  },
  { id: "approve_patch", label: "Approve patch", doneAt: "PATCH_APPROVED" },
  {
    id: "validate",
    label: "Validation",
    doneAt: "VALIDATION_PASSED",
    runningAt: ["VALIDATION_RUNNING"],
    failedAt: "VALIDATION_FAILED",
  },
  { id: "approve_pr", label: "Approve pull request", doneAt: "PR_APPROVED" },
  { id: "pr", label: "Pull request", doneAt: "PR_CREATED", runningAt: ["PR_CREATING"] },
];

/**
 * Each stage's status from the run state alone. A stage is `done` only when the
 * state has reached its `doneAt`. The first unfinished stage is `failed` when
 * the state is that stage's failed verdict or the server reports a failure
 * since the run last moved; `running` when the state says the server is inside
 * it; otherwise `current`. Every later stage is `not_yet`.
 *
 * `RUN_STATE_ORDER` puts each `*_FAILED` state before its `*_PASSED` state, so
 * a failed review or validation can never reach its stage's `doneAt`.
 */
export function stageStatuses(
  state: RunState,
  failure: string | null,
): { stage: StageDef; status: StageStatus }[] {
  let currentAssigned = false;
  return STAGES.map((stage) => {
    if (atLeast(state, stage.doneAt)) return { stage, status: "done" as const };
    if (!currentAssigned) {
      currentAssigned = true;
      if (failure || stage.failedAt === state) return { stage, status: "failed" as const };
      if (stage.runningAt?.includes(state)) return { stage, status: "running" as const };
      return { stage, status: "current" as const };
    }
    return { stage, status: "not_yet" as const };
  });
}

/** A failed call, stated with the server's own reason. A 503 names the executor. */
export function describeFailure(action: string, error: unknown): string {
  if (error instanceof ApiError) {
    if (error.kind === "unavailable") return `Executor unavailable: ${error.detail}`;
    return `${action} failed (${error.status}): ${error.detail}`;
  }
  return `${action} failed: ${error instanceof Error ? error.message : String(error)}`;
}

// -- payloads ---------------------------------------------------------------

export class MalformedPayloadError extends Error {
  constructor(what: string) {
    super(`The stored ${what} is not in the shape this screen reads.`);
    this.name = "MalformedPayloadError";
  }
}

export type Risk = "READ" | "WRITE" | "DESTRUCTIVE";

export interface EvidenceView {
  path: string;
  symbol: string | null;
  line: number | null;
}

/** `models/analysis.py::Workflow`. */
export interface WorkflowView {
  id: string;
  name: string;
  description: string;
  risk: Risk;
  primaryFunction: string;
  confidence: number;
  evidence: EvidenceView[];
}

/** `models/toolplan.py::ToolParameter`. */
export interface ParameterView {
  name: string;
  jsonType: string;
  description: string;
  required: boolean;
}

/** `models/toolplan.py::ToolPlanEntry`. */
export interface ToolView {
  name: string;
  title: string;
  description: string;
  workflowId: string;
  mapsToFunction: string;
  parameters: ParameterView[];
  outputDescription: string;
  risk: Risk;
  /** Set by `reconcile_risk` on the server, never by the model. */
  approvalRequired: boolean;
  evidence: EvidenceView[];
}

export interface PlanView {
  /** The TOOL_PLAN artifact hash the approval binds to. */
  hash: string;
  tools: ToolView[];
  notes: string[];
  /** Tool name to the types the binding could not type-check. */
  typesNotChecked: Record<string, string[]>;
}

/** Below this the analyst's evidence is weak (`Workflow.is_low_confidence`). */
export const LOW_CONFIDENCE = 0.6;

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(o: Obj, key: string, what: string): string {
  const v = o[key];
  if (typeof v !== "string") throw new MalformedPayloadError(what);
  return v;
}

function risk(o: Obj, what: string): Risk {
  const v = o.risk;
  if (v !== "READ" && v !== "WRITE" && v !== "DESTRUCTIVE") throw new MalformedPayloadError(what);
  return v;
}

function evidence(o: Obj, what: string): EvidenceView[] {
  const raw = o.evidence;
  if (!Array.isArray(raw)) throw new MalformedPayloadError(what);
  return raw.map((e) => {
    if (!isObj(e) || typeof e.path !== "string") throw new MalformedPayloadError(what);
    return {
      path: e.path,
      symbol: typeof e.symbol === "string" ? e.symbol : null,
      line: typeof e.line === "number" ? e.line : null,
    };
  });
}

/** The ANALYSIS artifact's workflows. `null` when analysis has not stored one. */
export function parseWorkflows(artifact: AgentArtifactDto): WorkflowView[] | null {
  if (!artifact.available) return null;
  const what = "analysis";
  const analysis = artifact.payload.analysis;
  if (!isObj(analysis) || !Array.isArray(analysis.workflows)) throw new MalformedPayloadError(what);
  return analysis.workflows.map((w) => {
    if (!isObj(w) || typeof w.confidence !== "number") throw new MalformedPayloadError(what);
    return {
      id: str(w, "id", what),
      name: str(w, "name", what),
      description: str(w, "description", what),
      risk: risk(w, what),
      primaryFunction: str(w, "primary_function", what),
      confidence: w.confidence,
      evidence: evidence(w, what),
    };
  });
}

/** The TOOL_PLAN artifact. `null` when no plan has been stored. */
export function parsePlan(artifact: AgentArtifactDto): PlanView | null {
  if (!artifact.available) return null;
  const what = "tool plan";
  const { plan, types_not_checked: unchecked } = artifact.payload;
  if (!artifact.artifact_hash || !isObj(plan) || !Array.isArray(plan.tools)) {
    throw new MalformedPayloadError(what);
  }

  const typesNotChecked: Record<string, string[]> = {};
  if (unchecked !== undefined) {
    if (!isObj(unchecked)) throw new MalformedPayloadError(what);
    for (const [tool, types] of Object.entries(unchecked)) {
      if (!Array.isArray(types) || !types.every((t) => typeof t === "string")) {
        throw new MalformedPayloadError(what);
      }
      typesNotChecked[tool] = types as string[];
    }
  }

  const tools = plan.tools.map((t): ToolView => {
    if (!isObj(t) || !Array.isArray(t.parameters) || typeof t.approval_required !== "boolean") {
      throw new MalformedPayloadError(what);
    }
    return {
      name: str(t, "name", what),
      title: str(t, "title", what),
      description: str(t, "description", what),
      workflowId: str(t, "workflow_id", what),
      mapsToFunction: str(t, "maps_to_function", what),
      outputDescription: str(t, "output_description", what),
      risk: risk(t, what),
      approvalRequired: t.approval_required,
      evidence: evidence(t, what),
      parameters: t.parameters.map((p) => {
        if (!isObj(p) || typeof p.required !== "boolean") throw new MalformedPayloadError(what);
        return {
          name: str(p, "name", what),
          jsonType: str(p, "json_type", what),
          description: str(p, "description", what),
          required: p.required,
        };
      }),
    };
  });

  const notes = Array.isArray(plan.notes)
    ? plan.notes.filter((n): n is string => typeof n === "string")
    : [];
  return { hash: artifact.artifact_hash, tools, notes, typesNotChecked };
}

// -- T5b reads ----------------------------------------------------------------
//
// The typed client says what the server should send; these check what it did
// send. A stage that has not run is `null` and stays `null`. Anything else that
// is not the backend model's shape is refused, never defaulted into a pass.

function bool(o: Obj, key: string, what: string): boolean {
  const v = o[key];
  if (typeof v !== "boolean") throw new MalformedPayloadError(what);
  return v;
}

function num(o: Obj, key: string, what: string): number {
  const v = o[key];
  if (typeof v !== "number" || !Number.isFinite(v)) throw new MalformedPayloadError(what);
  return v;
}

function strOrNull(o: Obj, key: string, what: string): string | null {
  const v = o[key];
  if (v === null || v === undefined) return null;
  if (typeof v !== "string") throw new MalformedPayloadError(what);
  return v;
}

function numOrNull(o: Obj, key: string, what: string): number | null {
  const v = o[key];
  if (v === null || v === undefined) return null;
  if (typeof v !== "number" || !Number.isFinite(v)) throw new MalformedPayloadError(what);
  return v;
}

function strList(o: Obj, key: string, what: string): string[] {
  const v = o[key];
  if (!Array.isArray(v) || !v.every((x) => typeof x === "string")) {
    throw new MalformedPayloadError(what);
  }
  return v as string[];
}

function oneOf<T extends string>(o: Obj, key: string, allowed: readonly T[], what: string): T {
  const v = o[key];
  if (typeof v !== "string" || !(allowed as readonly string[]).includes(v)) {
    throw new MalformedPayloadError(what);
  }
  return v as T;
}

/** `GET /pipeline/patch` — `PatchResponse`. */
export function parsePatch(raw: unknown): PatchDto | null {
  if (raw === null) return null;
  const what = "patch";
  if (!isObj(raw) || !Array.isArray(raw.files)) throw new MalformedPayloadError(what);
  const hash = str(raw, "artifact_hash", what);
  if (hash.length === 0) throw new MalformedPayloadError(what);
  return {
    artifact_hash: hash,
    summary: str(raw, "summary", what),
    base_commit: strOrNull(raw, "base_commit", what),
    total_added: num(raw, "total_added", what),
    total_removed: num(raw, "total_removed", what),
    files: raw.files.map((f): PatchFileDto => {
      if (!isObj(f)) throw new MalformedPayloadError(what);
      return {
        path: str(f, "path", what),
        kind: oneOf(f, "kind", ["add", "modify"] as const, what),
        rationale: str(f, "rationale", what),
        affectedTool: strOrNull(f, "affectedTool", what),
        diff: str(f, "diff", what),
        added: num(f, "added", what),
        removed: num(f, "removed", what),
      };
    }),
  };
}

const SEVERITIES = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"] as const;

/** `GET /pipeline/security-review` — `SecurityReviewResponse`. */
export function parseSecurityReview(raw: unknown): SecurityReviewDto | null {
  if (raw === null) return null;
  const what = "security review";
  if (!isObj(raw) || !Array.isArray(raw.findings)) throw new MalformedPayloadError(what);
  const agentSaid = raw.agent_said_pass;
  if (agentSaid !== null && agentSaid !== undefined && typeof agentSaid !== "boolean") {
    throw new MalformedPayloadError(what);
  }
  return {
    completed: bool(raw, "completed", what),
    passed: bool(raw, "passed", what),
    reason: str(raw, "reason", what),
    agent_said_pass: typeof agentSaid === "boolean" ? agentSaid : null,
    overridden: bool(raw, "overridden", what),
    findings: raw.findings.map((f): FindingDto => {
      if (!isObj(f)) throw new MalformedPayloadError(what);
      const ev = f.evidence;
      let evidenceView: FindingDto["evidence"] = null;
      if (ev !== null && ev !== undefined) {
        if (!isObj(ev) || typeof ev.path !== "string") throw new MalformedPayloadError(what);
        evidenceView = {
          path: ev.path,
          symbol: typeof ev.symbol === "string" ? ev.symbol : null,
          line: typeof ev.line === "number" ? ev.line : null,
        };
      }
      return {
        rule: str(f, "rule", what),
        severity: oneOf(f, "severity", SEVERITIES, what),
        summary: str(f, "summary", what),
        recommendation: str(f, "recommendation", what),
        evidence: evidenceView,
        deterministic: bool(f, "deterministic", what),
      };
    }),
  };
}

/** The review passed only if it completed *and* the gate passed it. */
export function reviewPassed(review: SecurityReviewDto | null): boolean {
  return review !== null && review.completed && review.passed;
}

/** `GET /pipeline/validation` — `ValidationResponse`. */
export function parseValidation(raw: unknown): ValidationDto | null {
  if (raw === null) return null;
  const what = "validation result";
  if (!isObj(raw) || !Array.isArray(raw.checks)) throw new MalformedPayloadError(what);

  let score: ValidationDto["score"] = null;
  if (raw.score !== null && raw.score !== undefined) {
    const s = raw.score;
    if (!isObj(s) || !Array.isArray(s.components)) throw new MalformedPayloadError(what);
    score = {
      total: num(s, "total", what),
      max_total: num(s, "max_total", what),
      components: s.components.map((c): ScoreComponentDto => {
        if (!isObj(c)) throw new MalformedPayloadError(what);
        return {
          component: str(c, "component", what),
          label: str(c, "label", what),
          weight: num(c, "weight", what),
          points: num(c, "points", what),
          checks_passed: num(c, "checks_passed", what),
          checks_executed: num(c, "checks_executed", what),
          detail: str(c, "detail", what),
        };
      }),
    };
  }

  return {
    completed: bool(raw, "completed", what),
    passed: bool(raw, "passed", what),
    validated: bool(raw, "validated", what),
    summary: strOrNull(raw, "summary", what),
    reason: strOrNull(raw, "reason", what),
    failed_check_ids: strList(raw, "failed_check_ids", what),
    unexecuted_tool_checks: strList(raw, "unexecuted_tool_checks", what),
    checks: raw.checks.map((c): ValidationCheckDto => {
      if (!isObj(c)) throw new MalformedPayloadError(what);
      const timedOut = c.timed_out;
      if (timedOut !== null && timedOut !== undefined && typeof timedOut !== "boolean") {
        throw new MalformedPayloadError(what);
      }
      return {
        check_id: str(c, "check_id", what),
        component: strOrNull(c, "component", what),
        description: str(c, "description", what),
        status: oneOf(c, "status", ["passed", "failed", "skipped"] as const, what),
        exit_code: numOrNull(c, "exit_code", what),
        timed_out: typeof timedOut === "boolean" ? timedOut : null,
        duration_seconds: numOrNull(c, "duration_seconds", what),
        stdout_excerpt: str(c, "stdout_excerpt", what),
        stderr_excerpt: str(c, "stderr_excerpt", what),
        skip_reason: strOrNull(c, "skip_reason", what),
      };
    }),
    score,
    dependency_source: strOrNull(raw, "dependency_source", what),
    dependency_detail: strOrNull(raw, "dependency_detail", what),
  };
}

/** Validation passed only if it ran, the pipeline passed it, and every tool check ran. */
export function validationPassed(validation: ValidationDto | null): boolean {
  return (
    validation !== null && validation.completed && validation.passed && validation.validated
  );
}

/** `GET /pipeline/pull-request` — `PullRequestResponse`. */
export function parsePullRequest(raw: unknown): PullRequestDto | null {
  if (raw === null) return null;
  const what = "pull request record";
  if (!isObj(raw)) throw new MalformedPayloadError(what);
  return {
    status: oneOf(raw, "status", ["AWAITING_APPROVAL", "CREATING", "FAILED", "OPENED"] as const, what),
    url: strOrNull(raw, "url", what),
    number: numOrNull(raw, "number", what),
    branch: strOrNull(raw, "branch", what),
    failure: strOrNull(raw, "failure", what),
  };
}

/** A URL safe to put in an `href`: https only, so a stored value can never be `javascript:`. */
export function safeHttpsUrl(url: string | null): string | null {
  if (url === null) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}
