/**
 * Typed API client.
 *
 * Every request carries a fresh ID token. The backend verifies it — the client
 * never asserts who it is, it only presents a token.
 */

import { env } from "@/lib/env";
import type {
  AccessDto,
  AgentArtifactDto,
  AgentStatusDto,
  ApprovalDto,
  ApprovalGate,
  ApprovalStatus,
  AwaitingApprovalDto,
  ChatEvent,
  EventDto,
  PatchDto,
  PipelineConnectBody,
  PipelineStepDto,
  PipelineWorkflowsBody,
  ProjectDto,
  PullRequestDto,
  RunStateDto,
  SecurityReviewDto,
  ValidationDto,
  RepositoryDto,
  SessionDto,
  StageDto,
  TrustStateDto,
  TurnDto,
} from "@/lib/api/types";

/**
 * What a failed call means, so the UI can say it plainly. `not_found` is a
 * missing session or resource; a stage that has not run is a `null` read, not
 * an error.
 */
export type ApiErrorKind =
  | "unauthenticated" // 401
  | "approval_required" // 403
  | "not_found" // 404
  | "conflict" // 409
  | "invalid" // 422
  | "unavailable" // 503 — `detail` carries the server's stated reason
  | "failed"; // anything else

export function errorKindOf(status: number): ApiErrorKind {
  switch (status) {
    case 401:
      return "unauthenticated";
    case 403:
      return "approval_required";
    case 404:
      return "not_found";
    case 409:
      return "conflict";
    case 422:
      return "invalid";
    case 503:
      return "unavailable";
    default:
      return "failed";
  }
}

/** FastAPI's `{"detail": ...}`, or the raw text when the body is not that. */
function detailOf(body: string): string {
  try {
    const parsed = JSON.parse(body) as unknown;
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const { detail } = parsed as { detail: unknown };
      return typeof detail === "string" ? detail : JSON.stringify(detail);
    }
  } catch {
    // Not JSON: the raw text is the detail.
  }
  return body;
}

export class ApiError extends Error {
  /** The server's own reason, unwrapped from `{"detail": ...}`. */
  readonly detail: string;
  readonly kind: ApiErrorKind;

  /** `message` stays the raw response body, as it always has been. */
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
    this.detail = detailOf(message);
    this.kind = errorKindOf(status);
  }
}

export type TokenSource = () => Promise<string>;

export class ApiClient {
  constructor(
    private readonly getToken: TokenSource,
    private readonly baseUrl: string = env.apiBaseUrl,
  ) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const token = await this.getToken();
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        ...init.headers,
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
    });

    if (!response.ok) {
      // Surface the real reason. Never a generic failure message.
      const detail = await response.text().catch(() => "");
      throw new ApiError(response.status, detail || response.statusText);
    }
    return (await response.json()) as T;
  }

  listProjects(): Promise<ProjectDto[]> {
    return this.request<ProjectDto[]>("/api/projects");
  }

  createProject(name: string): Promise<ProjectDto> {
    return this.request<ProjectDto>("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
  }

  createSession(projectId: string): Promise<SessionDto> {
    return this.request<SessionDto>(`/api/projects/${projectId}/sessions`, { method: "POST" });
  }

  listTurns(sessionId: string): Promise<TurnDto[]> {
    return this.request<TurnDto[]>(`/api/sessions/${sessionId}/turns`);
  }

  listEvents(sessionId: string): Promise<EventDto[]> {
    return this.request<EventDto[]>(`/api/sessions/${sessionId}/events`);
  }

  getApproval(approvalId: string): Promise<ApprovalDto> {
    return this.request<ApprovalDto>(`/api/approvals/${approvalId}`);
  }

  decideApproval(approvalId: string, decision: ApprovalStatus): Promise<ApprovalDto> {
    return this.request<ApprovalDto>(`/api/approvals/${approvalId}/decide`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    });
  }

  checkGate(
    sessionId: string,
    gate: ApprovalGate,
    artifactHash: string,
  ): Promise<{ open: boolean; reason: string }> {
    const query = new URLSearchParams({ gate, artifact_hash: artifactHash });
    return this.request(`/api/sessions/${sessionId}/gate?${query}`);
  }

  // -- the agent surface -------------------------------------------------
  //
  // These back the WebMCP tools in `src/webmcp/`. They are ordinary authorised
  // calls: the server enforces ownership and stamps AGENT origin from the route,
  // so nothing here needs to be trusted.

  agentStatus(sessionId: string): Promise<AgentStatusDto> {
    return this.request<AgentStatusDto>(`/api/agent/sessions/${sessionId}/status`);
  }

  agentWorkflows(sessionId: string): Promise<AgentArtifactDto> {
    return this.request<AgentArtifactDto>(`/api/agent/sessions/${sessionId}/workflows`);
  }

  agentPlan(sessionId: string): Promise<AgentArtifactDto> {
    return this.request<AgentArtifactDto>(`/api/agent/sessions/${sessionId}/plan`);
  }

  agentValidation(sessionId: string): Promise<AgentArtifactDto> {
    return this.request<AgentArtifactDto>(`/api/agent/sessions/${sessionId}/validation`);
  }

  agentStartAnalysis(sessionId: string): Promise<StageDto> {
    return this.request<StageDto>(`/api/agent/sessions/${sessionId}/analysis`, { method: "POST" });
  }

  agentConnectRepository(
    sessionId: string,
    repositoryFullName: string,
    branch: string,
  ): Promise<AwaitingApprovalDto> {
    return this.request<AwaitingApprovalDto>(`/api/agent/sessions/${sessionId}/repository`, {
      method: "POST",
      body: JSON.stringify({ repository_full_name: repositoryFullName, branch }),
    });
  }

  agentSelectWorkflows(sessionId: string, workflowIds: string[]): Promise<AwaitingApprovalDto> {
    return this.request<AwaitingApprovalDto>(`/api/agent/sessions/${sessionId}/workflows`, {
      method: "POST",
      body: JSON.stringify({ workflow_ids: workflowIds }),
    });
  }

  /** Requests the plan approval. It does not grant one — only a human can. */
  agentRequestPlanApproval(sessionId: string): Promise<AwaitingApprovalDto> {
    return this.request<AwaitingApprovalDto>(`/api/agent/sessions/${sessionId}/plan/approve`, {
      method: "POST",
    });
  }

  agentGeneratePatch(sessionId: string, summary: string): Promise<StageDto> {
    return this.request<StageDto>(`/api/agent/sessions/${sessionId}/patch`, {
      method: "POST",
      body: JSON.stringify({ summary }),
    });
  }

  agentSecurityReview(sessionId: string): Promise<StageDto> {
    return this.request<StageDto>(`/api/agent/sessions/${sessionId}/security-review`, {
      method: "POST",
    });
  }

  agentRunValidation(sessionId: string): Promise<StageDto> {
    return this.request<StageDto>(`/api/agent/sessions/${sessionId}/validation`, {
      method: "POST",
    });
  }

  agentCreatePullRequest(
    sessionId: string,
    title: string,
    body = "",
  ): Promise<AwaitingApprovalDto> {
    return this.request<AwaitingApprovalDto>(`/api/agent/sessions/${sessionId}/pull-request`, {
      method: "POST",
      body: JSON.stringify({ title, body }),
    });
  }

  // -- the pipeline (F9-01 / T4) -----------------------------------------
  //
  // The developer's own actions. The server records them as HUMAN and moves a
  // gate only on an approval already decided in `/api/approvals` — nothing sent
  // here can grant one. Reads return `null` for a stage that has not run.

  private pipelinePost(
    sessionId: string,
    step: string,
    body?: object,
  ): Promise<PipelineStepDto> {
    return this.request<PipelineStepDto>(`/api/sessions/${sessionId}/pipeline/${step}`, {
      method: "POST",
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  }

  private pipelineGet<T>(sessionId: string, stage: string): Promise<T> {
    return this.request<T>(`/api/sessions/${sessionId}/pipeline/${stage}`);
  }

  pipelineConnect(sessionId: string, body?: PipelineConnectBody): Promise<PipelineStepDto> {
    return this.pipelinePost(sessionId, "connect", body);
  }

  pipelineAnalyze(sessionId: string): Promise<PipelineStepDto> {
    return this.pipelinePost(sessionId, "analysis");
  }

  pipelineSelectWorkflows(sessionId: string, body: PipelineWorkflowsBody): Promise<PipelineStepDto> {
    return this.pipelinePost(sessionId, "workflows", body);
  }

  pipelinePlan(sessionId: string): Promise<PipelineStepDto> {
    return this.pipelinePost(sessionId, "plan");
  }

  /** `approvalId` leaves the TOOL_PLAN gate; omit it to retry a running step. */
  pipelineGenerate(sessionId: string, approvalId?: string): Promise<PipelineStepDto> {
    return this.pipelinePost(
      sessionId,
      "patch",
      approvalId === undefined ? undefined : { approval_id: approvalId },
    );
  }

  pipelineSecurityReview(sessionId: string): Promise<PipelineStepDto> {
    return this.pipelinePost(sessionId, "security-review");
  }

  pipelineValidate(sessionId: string, approvalId: string): Promise<PipelineStepDto> {
    return this.pipelinePost(sessionId, "validation", { approval_id: approvalId });
  }

  pipelineRequestPullRequest(sessionId: string): Promise<PipelineStepDto> {
    return this.pipelinePost(sessionId, "pull-request/request");
  }

  /** `approvalId` leaves the PULL_REQUEST gate; omit it to retry a running step. */
  pipelineCreatePullRequest(sessionId: string, approvalId?: string): Promise<PipelineStepDto> {
    return this.pipelinePost(
      sessionId,
      "pull-request",
      approvalId === undefined ? undefined : { approval_id: approvalId },
    );
  }

  pipelineReject(sessionId: string, approvalId: string): Promise<PipelineStepDto> {
    return this.pipelinePost(sessionId, "reject", { approval_id: approvalId });
  }

  pipelineState(sessionId: string): Promise<RunStateDto> {
    return this.pipelineGet<RunStateDto>(sessionId, "state");
  }

  pipelinePatch(sessionId: string): Promise<PatchDto | null> {
    return this.pipelineGet<PatchDto | null>(sessionId, "patch");
  }

  pipelineSecurityReviewResult(sessionId: string): Promise<SecurityReviewDto | null> {
    return this.pipelineGet<SecurityReviewDto | null>(sessionId, "security-review");
  }

  pipelineValidationResult(sessionId: string): Promise<ValidationDto | null> {
    return this.pipelineGet<ValidationDto | null>(sessionId, "validation");
  }

  pipelinePullRequest(sessionId: string): Promise<PullRequestDto | null> {
    return this.pipelineGet<PullRequestDto | null>(sessionId, "pull-request");
  }

  // -- repositories and access (F7-05) -----------------------------------

  listRepositories(): Promise<RepositoryDto[]> {
    return this.request<RepositoryDto[]>("/api/github/repositories");
  }

  bindRepository(
    projectId: string,
    repositoryId: string,
    fullName: string,
    baseBranch: string,
  ): Promise<AccessDto> {
    return this.request<AccessDto>(`/api/projects/${projectId}/repository`, {
      method: "POST",
      body: JSON.stringify({
        repository_id: repositoryId,
        full_name: fullName,
        base_branch: baseBranch,
      }),
    });
  }

  /**
   * Widen a project to WRITE_PR. It takes no body: the reason is shown to the
   * developer in the UI, and the decision is the click. Nothing the client
   * sends influences whether elevation is permitted — the boundary decides.
   */
  elevateAccess(projectId: string): Promise<AccessDto> {
    return this.request<AccessDto>(`/api/projects/${projectId}/access/elevate`, {
      method: "POST",
    });
  }

  /**
   * Trust panel state — F8-03. Read-only, and the only source the panel has for
   * everything except the browser's WebMCP support, which no server can know.
   */
  getTrust(sessionId: string): Promise<TrustStateDto> {
    return this.request<TrustStateDto>(`/api/sessions/${sessionId}/trust`);
  }

  revokeAccess(projectId: string): Promise<AccessDto> {
    return this.request<AccessDto>(`/api/projects/${projectId}/access/revoke`, {
      method: "POST",
    });
  }

  /**
   * Streams a chat reply. `signal` cancels it, which also cancels the upstream
   * model call rather than leaving it running.
   */
  async *chat(
    sessionId: string,
    message: string,
    signal?: AbortSignal,
  ): AsyncGenerator<ChatEvent> {
    const token = await this.getToken();
    const response = await fetch(`${this.baseUrl}/api/sessions/${sessionId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ message }),
      signal,
    });

    if (!response.ok || !response.body) {
      const detail = await response.text().catch(() => "");
      throw new ApiError(response.status, detail || response.statusText);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let split = buffer.indexOf("\n\n");
        while (split !== -1) {
          const block = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const parsed = parseSseBlock(block);
          if (parsed) yield parsed;
          split = buffer.indexOf("\n\n");
        }
      }
    } finally {
      // Runs when the consumer stops early, aborts, or throws. Without this the
      // response body stays open and the server keeps streaming into nothing.
      await reader.cancel().catch(() => {});
    }
  }
}

export function parseSseBlock(block: string): ChatEvent | null {
  let name = "";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event: ")) name = line.slice(7).trim();
    else if (line.startsWith("data: ")) data = line.slice(6);
  }
  if (!name || !data) return null;

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(data) as Record<string, unknown>;
  } catch {
    return null;
  }

  switch (name) {
    case "turn":
      return { type: "turn", id: String(payload.id), role: String(payload.role) };
    case "activity":
      return {
        type: "activity",
        id: String(payload.id),
        kind: String(payload.kind),
        label: String(payload.label),
      };
    case "delta":
      return { type: "delta", text: String(payload.text) };
    case "error":
      return {
        type: "error",
        message: String(payload.message),
        kind: String(payload.kind ?? "unknown"),
      };
    case "done":
      return { type: "done", sessionId: String(payload.session_id) };
    default:
      return null;
  }
}
