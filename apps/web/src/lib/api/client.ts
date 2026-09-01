/**
 * Typed API client.
 *
 * Every request carries a fresh ID token. The backend verifies it — the client
 * never asserts who it is, it only presents a token.
 */

import { env } from "@/lib/env";
import type {
  ApprovalDto,
  ApprovalGate,
  ApprovalStatus,
  ChatEvent,
  EventDto,
  ProjectDto,
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
