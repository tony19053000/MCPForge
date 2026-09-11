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

// -- the pipeline (`/api/sessions/{id}/pipeline`, F9-01 / T3) --------------
//
// Mirrors `services/api/src/mcpforge/api/pipeline.py`. Datetimes arrive as ISO
// strings. A read that returns `null` means the stage has not run — never a
// default — so every read type below is `T | null` at the client boundary.

export type ArtifactKind =
  | "ANALYSIS"
  | "REPOSITORY_BINDING"
  | "WORKFLOW_SELECTION"
  | "TOOL_PLAN"
  | "PATCH"
  | "SECURITY_REVIEW"
  | "VALIDATION";

/** `PipelineStepResponse`. `approval`, when set, is the gate this step opened — always PENDING. */
export interface PipelineStepDto {
  session_id: string;
  state: RunState;
  detail: string;
  approval: ApprovalDto | null;
  pull_request_url: string | null;
}

/** `ConnectBody`. */
export interface PipelineConnectBody {
  repository_binding_approval_id?: string | null;
}

/** `WorkflowsBody`: exactly one of the two. The server enforces that, not this type. */
export type PipelineWorkflowsBody = { workflow_ids: string[] } | { approval_id: string };

export interface PendingGateDto {
  gate: ApprovalGate;
  artifact_kind: ArtifactKind;
  artifact_hash: string | null;
  /** May already be decided — the run moves only when a pipeline POST consumes it. */
  approval: ApprovalDto | null;
}

export interface RunStateDto {
  session_id: string;
  project_id: string;
  state: RunState;
  updated_at: string;
  pending_gate: PendingGateDto | null;
  failure: string | null;
}

export type ChangeKind = "add" | "modify";

/**
 * `DiffFileResponse`. Structurally a `DiffFile` from
 * `components/diff/diff-view.tsx` (camelCase `affectedTool` is the server's
 * serialization alias), plus `kind`.
 */
export interface PatchFileDto {
  path: string;
  kind: ChangeKind;
  rationale: string;
  affectedTool: string | null;
  diff: string;
  added: number;
  removed: number;
}

export interface PatchDto {
  /** The hash the PATCH and PULL_REQUEST approvals bind to. */
  artifact_hash: string;
  summary: string;
  base_commit: string | null;
  total_added: number;
  total_removed: number;
  files: PatchFileDto[];
}

export type Severity = "INFO" | "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

export interface FindingDto {
  rule: string;
  severity: Severity;
  summary: string;
  recommendation: string;
  evidence: { path: string; symbol: string | null; line: number | null } | null;
  /** True for findings from our own policy engine rather than the model. */
  deterministic: boolean;
}

export interface SecurityReviewDto {
  completed: boolean;
  /** The deterministic gate's verdict. The model's own view is `agent_said_pass`. */
  passed: boolean;
  reason: string;
  agent_said_pass: boolean | null;
  overridden: boolean;
  findings: FindingDto[];
}

export interface ValidationCheckDto {
  check_id: string;
  component: string | null;
  description: string;
  status: "passed" | "failed" | "skipped";
  exit_code: number | null;
  timed_out: boolean | null;
  duration_seconds: number | null;
  stdout_excerpt: string;
  stderr_excerpt: string;
  skip_reason: string | null;
}

export interface ScoreComponentDto {
  component: string;
  label: string;
  weight: number;
  points: number;
  checks_passed: number;
  checks_executed: number;
  detail: string;
}

export interface ValidationDto {
  /** False when validation could not run at all; then nothing below is evidence. */
  completed: boolean;
  passed: boolean;
  validated: boolean;
  summary: string | null;
  reason: string | null;
  failed_check_ids: string[];
  unexecuted_tool_checks: string[];
  checks: ValidationCheckDto[];
  score: { total: number; max_total: number; components: ScoreComponentDto[] } | null;
  dependency_source: string | null;
  dependency_detail: string | null;
}

export interface PullRequestDto {
  status: "AWAITING_APPROVAL" | "CREATING" | "FAILED" | "OPENED";
  url: string | null;
  number: number | null;
  branch: string | null;
  failure: string | null;
}

// -- trust panel (F8-03, 04_FRONTEND_SPEC.md §8) ---------------------------

/**
 * The execution boundary as an enum, never a boolean. Flattening it to
 * `attested: true|false` on the way to the screen is exactly the mistake
 * `02_ARCHITECTURE.md` §8 forbids.
 */
export type TrustLevel = "DEVELOPMENT_ISOLATION" | "HARDWARE_ATTESTED";

export interface AttestationEvidenceDto {
  issuer: string;
  audience: string;
  subject: string;
  image_digest: string;
  image_reference: string;
  workload_service_account: string;
  hardware_model: string;
  software_name: string;
  debug_status: string;
  issued_at: string;
  expires_at: string;
  verified_at: string;
}

export interface SecureExecutionDto {
  trust_level: TrustLevel;
  configured_executor: "development" | "confidential_space";
  provider_running: boolean;
  evidence: AttestationEvidenceDto | null;
  detail: string;
}

/**
 * `quarantined_count` is `null` — not `0` — until an analysis has actually run.
 * Zero would read as "scanned and clean" for a scan that never happened.
 */
export interface SecretFilteringDto {
  active: boolean;
  rule_count: number;
  analyzed: boolean;
  quarantined_count: number | null;
  /** Paths only. Contents never leave the server, and never existed here. */
  quarantined_paths: string[];
}

export interface TrustStateDto {
  session_id: string;
  project_id: string;
  repository: {
    bound: boolean;
    repository_full_name: string | null;
    base_branch: string | null;
    is_demo: boolean;
  };
  access_mode: "READ_ONLY" | "WRITE_PR";
  secret_filtering: SecretFilteringDto;
  secure_execution: SecureExecutionDto;
  branch_protection: {
    branch_prefix: string;
    branch_shape: string;
    protected_names: string[];
  };
}
