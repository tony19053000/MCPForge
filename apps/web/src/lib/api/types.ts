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
export type ApprovalGate =
  | "TOOL_PLAN"
  | "PATCH"
  | "PULL_REQUEST"
  | "ACCESS_ELEVATION"
  | "REPOSITORY_BINDING"
  | "WORKFLOW_SELECTION";
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

// -- the agent surface (`/api/agent`) --------------------------------------

export interface AgentStatusDto {
  project_id: string;
  name: string;
  state: RunState;
  access_mode: "READ_ONLY" | "WRITE_PR";
  repository_full_name: string | null;
  is_demo: boolean;
  session_id: string;
}

/** A stored artifact. `available: false` means the step has not run yet. */
export interface AgentArtifactDto {
  available: boolean;
  kind: string;
  artifact_hash: string | null;
  payload: Record<string, unknown>;
}

/**
 * The only thing a mutation tool returns. There is deliberately no success
 * variant: calling one asks a human to decide, it does not cause the action.
 */
export interface AwaitingApprovalDto {
  status: "awaiting_human_approval";
  approval_id: string;
  gate: ApprovalGate;
  artifact_hash: string;
  message: string;
}

export interface RepositoryDto {
  id: string;
  full_name: string;
  default_branch: string;
  private: boolean;
}

export interface AccessDto {
  project_id: string;
  access_mode: "READ_ONLY" | "WRITE_PR";
  repository_full_name: string | null;
  base_branch: string | null;
  elevated_by: string | null;
  elevated_at: string | null;
}

/**
 * A pipeline stage the agent asked for. `started` is false while the stage is
 * not connected to the orchestrator — reporting true would be a hardcoded value
 * that makes a check look passed.
 */
export interface StageDto {
  session_id: string;
  state: RunState;
  started: boolean;
  detail: string;
}
