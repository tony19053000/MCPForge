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
  ProjectDto,
  RepositoryDto,
  SessionDto,
  TurnDto,
} from "@/lib/api/types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
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

  agentStartAnalysis(sessionId: string): Promise<{ session_id: string; started: boolean }> {
    return this.request(`/api/agent/sessions/${sessionId}/analysis`, { method: "POST" });
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

  agentGeneratePatch(sessionId: string, summary: string): Promise<AwaitingApprovalDto> {
    return this.request<AwaitingApprovalDto>(`/api/agent/sessions/${sessionId}/patch`, {
      method: "POST",
      body: JSON.stringify({ summary }),
    });
  }

  agentSecurityReview(sessionId: string): Promise<AgentArtifactDto> {
    return this.request<AgentArtifactDto>(`/api/agent/sessions/${sessionId}/security-review`, {
      method: "POST",
    });
  }

  agentRunValidation(sessionId: string): Promise<AgentArtifactDto> {
    return this.request<AgentArtifactDto>(`/api/agent/sessions/${sessionId}/validation`, {
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
