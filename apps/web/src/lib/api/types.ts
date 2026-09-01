/**
 * API payload types.
 *
 * These mirror the backend's Pydantic models. 02_ARCHITECTURE.md §1.2 makes the
 * backend the source of truth; once `npm run gen:api-types` exists these are
 * generated from the OpenAPI schema instead of hand-written.
 */

export type RunState =
  | "PROJECT_CREATED"
  | "REPOSITORY_CONNECTED"
  | "ANALYSIS_PENDING"
  | "ANALYSIS_RUNNING"
  | "ANALYSIS_COMPLETE"
  | "WORKFLOW_SELECTION_PENDING"
  | "WORKFLOWS_SELECTED"
  | "TOOL_PLAN_RUNNING"
  | "TOOL_PLAN_READY"
  | "TOOL_PLAN_APPROVAL_PENDING"
  | "TOOL_PLAN_APPROVED"
  | "GENERATION_RUNNING"
  | "PATCH_READY"
  | "SECURITY_REVIEW_RUNNING"
  | "SECURITY_REVIEW_FAILED"
  | "SECURITY_REVIEW_PASSED"
  | "PATCH_APPROVAL_PENDING"
  | "PATCH_APPROVED"
  | "VALIDATION_RUNNING"
  | "VALIDATION_FAILED"
  | "VALIDATION_PASSED"
  | "PR_APPROVAL_PENDING"
  | "PR_APPROVED"
  | "PR_CREATING"
  | "PR_CREATED"
  | "COMPLETE";

export type Origin = "HUMAN" | "AGENT" | "SYSTEM";
export type ApprovalGate = "TOOL_PLAN" | "PATCH" | "PULL_REQUEST" | "ACCESS_ELEVATION";
export type ApprovalStatus = "PENDING" | "APPROVED" | "REJECTED";

export interface ProjectDto {
  id: string;
  name: string;
  access_mode: "READ_ONLY" | "WRITE_PR";
  repository_full_name: string | null;
  is_demo: boolean;
}

export interface SessionDto {
  id: string;
  project_id: string;
  title: string;
  state: RunState;
}

export interface TurnDto {
  id: string;
  role: "user" | "assistant";
  text: string;
  origin: Origin;
}

export interface EventDto {
  id: string;
  kind: string;
  label: string;
  detail: Record<string, unknown>;
  origin: Origin;
  created_at: string;
}

export interface ApprovalDto {
  id: string;
  gate: ApprovalGate;
  artifact_hash: string;
  summary: string;
  status: ApprovalStatus;
  requested_at: string;
  decided_at: string | null;
  actor_uid: string | null;
}

/** What the SSE stream can send. Never model reasoning. */
export type ChatEvent =
  | { type: "turn"; id: string; role: string }
  | { type: "activity"; id: string; kind: string; label: string }
  | { type: "delta"; text: string }
  | { type: "error"; message: string; kind: string }
  | { type: "done"; sessionId: string };
