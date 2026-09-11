/**
 * The journey's pure logic — ticket T5a.
 *
 * Nothing here holds state. Every function reads a server payload and says
 * what it means; the backend run state is the only source of truth for whether
 * a stage is done. A payload that does not have the shape the backend models
 * define is refused with `MalformedPayloadError`, never filled with defaults.
 */

import { ApiError } from "@/lib/api/client";
import type { AgentArtifactDto, RunState } from "@/lib/api/types";

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

export type StageStatus = "done" | "current" | "failed" | "not_yet";

export interface StageDef {
  id: string;
  label: string;
  /** The first run state that means this stage has finished. */
  doneAt: RunState;
  /** Stages this screen does not drive yet (T5b). They are only ever "not yet" or "done". */
  later?: boolean;
}

export const STAGES: readonly StageDef[] = [
  { id: "connect", label: "Connect repository", doneAt: "ANALYSIS_PENDING" },
  { id: "analyze", label: "Analyze", doneAt: "ANALYSIS_COMPLETE" },
  { id: "workflows", label: "Select workflows", doneAt: "WORKFLOWS_SELECTED" },
  { id: "plan", label: "Design tool plan", doneAt: "TOOL_PLAN_READY" },
  { id: "approve", label: "Approve tool plan", doneAt: "TOOL_PLAN_APPROVED" },
  { id: "generate", label: "Generate code", doneAt: "PATCH_READY", later: true },
  { id: "review", label: "Security review", doneAt: "SECURITY_REVIEW_PASSED", later: true },
  { id: "validate", label: "Validation", doneAt: "VALIDATION_PASSED", later: true },
  { id: "pr", label: "Pull request", doneAt: "PR_CREATED", later: true },
];

/**
 * Each stage's status from the run state alone. A stage is `done` only when the
 * state has reached its `doneAt`; the first unfinished T5a stage is `current`,
 * or `failed` when the server reports a failure since the run last moved.
 */
export function stageStatuses(
  state: RunState,
  failure: string | null,
): { stage: StageDef; status: StageStatus }[] {
  let currentAssigned = false;
  return STAGES.map((stage) => {
    if (atLeast(state, stage.doneAt)) return { stage, status: "done" as const };
    if (!currentAssigned && !stage.later) {
      currentAssigned = true;
      return { stage, status: failure ? ("failed" as const) : ("current" as const) };
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
