/**
 * MCPForge's own WebMCP tools — F7-02 (read) and F7-03 (gated mutation).
 *
 * Every tool is a thin call onto `ApiClient`. That is deliberate and is the
 * security design, not laziness:
 *
 * - **Tools never enforce ownership.** They do not filter results by user. The
 *   server's store scopes every query to the verified token's subject, so a
 *   tool that forgot to filter still cannot read another user's data.
 * - **Tools never decide anything.** The mutation tools return
 *   `awaiting_human_approval`. There is no success variant to return, because
 *   calling one does not cause the action — it asks a person to decide.
 * - **Errors are structured, never thrown at the agent.** An unauthenticated or
 *   forbidden call comes back as `{ error }` so the agent gets a usable answer
 *   instead of a stack trace, and never partial data.
 */

import { ApiError, type ApiClient } from "@/lib/api/client";
import type { ModelContextTool } from "@/webmcp/adapter";

/** What every tool returns on failure. Data and errors are never mixed. */
export interface ToolError {
  error: string;
  code: "unauthenticated" | "forbidden" | "not_found" | "conflict" | "failed";
}

function classify(status: number): ToolError["code"] {
  if (status === 401) return "unauthenticated";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  return "failed";
}

/**
 * Runs a tool body and converts any failure into a structured result.
 *
 * A rejected promise would surface to the agent as an exception with whatever
 * detail the error carried. This keeps the shape predictable and the message
 * ours.
 */
export async function safely<T>(run: () => Promise<T>): Promise<T | ToolError> {
  try {
    return await run();
  } catch (cause) {
    if (cause instanceof ApiError) {
      return { error: cause.message, code: classify(cause.status) };
    }
    return { error: "The request could not be completed.", code: "failed" };
  }
}

const NO_INPUT = {
  type: "object",
  properties: {},
  additionalProperties: false,
} as const;

/**
 * Builds every MCPForge tool for one session.
 *
 * The session id is closed over rather than taken as a tool input: an agent
 * must not be able to point a tool at a different session, and the server would
 * reject it anyway, but there is no reason to offer the parameter.
 */
export function createTools(
  client: ApiClient,
  sessionId: string,
): ModelContextTool[] {
  return [...readTools(client, sessionId), ...mutationTools(client, sessionId)];
}

// -- F7-02 read tools -------------------------------------------------------

export function readTools(
  client: ApiClient,
  sessionId: string,
): ModelContextTool[] {
  return [
    {
      name: "get_project_status",
      title: "Get project status",
      description:
        "Get the MCPForge project's current state: its name, run state, repository " +
        "binding and whether access is read-only.",
      inputSchema: NO_INPUT,
      execute: () => safely(() => client.agentStatus(sessionId)),
    },
    {
      name: "list_detected_workflows",
      title: "List detected workflows",
      description:
        "List the user workflows MCPForge found in the connected repository. Returns " +
        "available: false when analysis has not run yet.",
      inputSchema: NO_INPUT,
      execute: () => safely(() => client.agentWorkflows(sessionId)),
    },
    {
      name: "get_webmcp_plan",
      title: "Get the WebMCP tool plan",
      description:
        "Get the proposed WebMCP tools, their risk levels and which workflow each " +
        "comes from. Returns available: false before a plan exists.",
      inputSchema: NO_INPUT,
      execute: () => safely(() => client.agentPlan(sessionId)),
    },
    {
      name: "get_validation_report",
      title: "Get the validation report",
      description:
        "Get the result of running the generated tests in the sandbox: what passed, " +
        "what failed, and the readiness score.",
      inputSchema: NO_INPUT,
      execute: () => safely(() => client.agentValidation(sessionId)),
    },
    {
      name: "start_repository_analysis",
      title: "Start repository analysis",
      description:
        "Request analysis of the connected repository. Not yet connected to the " +
        "orchestrator: the request is recorded and started is false. Analysis only " +
        "reads the repository and writes nothing to it.",
      inputSchema: NO_INPUT,
      execute: () => safely(() => client.agentStartAnalysis(sessionId)),
    },
  ];
}

// -- F7-03 gated mutation tools ---------------------------------------------

/**
 * The sentence every mutation tool puts in front of the agent.
 *
 * It is on the tool description, not only in the result, so a well-behaved
 * agent knows before calling that it is requesting rather than doing.
 */
const NEEDS_APPROVAL =
  " This does not perform the action. It asks the developer to approve it in " +
  "MCPForge, and returns an approval id. An agent cannot approve its own request.";

export function mutationTools(
  client: ApiClient,
  sessionId: string,
): ModelContextTool[] {
  return [
    {
      name: "connect_project",
      title: "Request connecting a repository",
      description:
        "Request binding this project to a GitHub repository and branch." +
        NEEDS_APPROVAL,
      inputSchema: {
        type: "object",
        properties: {
          repository_full_name: { type: "string", description: "owner/name" },
          branch: { type: "string", description: "The branch to analyse." },
        },
        required: ["repository_full_name", "branch"],
        additionalProperties: false,
      },
      execute: (input) =>
        safely(() =>
          client.agentConnectRepository(
            sessionId,
            String(input.repository_full_name ?? ""),
            String(input.branch ?? ""),
          ),
        ),
    },
    {
      name: "select_workflows",
      title: "Request exposing workflows as tools",
      description:
        "Request that the listed workflows become WebMCP tools." +
        NEEDS_APPROVAL,
      inputSchema: {
        type: "object",
        properties: {
          workflow_ids: {
            type: "array",
            items: { type: "string" },
            minItems: 1,
          },
        },
        required: ["workflow_ids"],
        additionalProperties: false,
      },
      execute: (input) =>
        safely(() =>
          client.agentSelectWorkflows(
            sessionId,
            (input.workflow_ids as string[]) ?? [],
          ),
        ),
    },
    {
      name: "approve_webmcp_plan",
      title: "Request approval of the tool plan",
      description:
        "Ask the developer to approve the current WebMCP tool plan. Named for the " +
        "intent; the effect is a request." +
        NEEDS_APPROVAL,
      inputSchema: NO_INPUT,
      execute: () => safely(() => client.agentRequestPlanApproval(sessionId)),
    },
    {
      name: "generate_patch",
      title: "Request patch generation",
      description:
        "Request generation of the WebMCP code patch. Authorised by the approved tool " +
        "plan, so it opens no gate of its own. Not yet connected to the generator: the " +
        "request is recorded and started is false.",
      inputSchema: {
        type: "object",
        properties: {
          summary: { type: "string", description: "Why this patch is wanted." },
        },
        additionalProperties: false,
      },
      execute: (input) =>
        safely(() =>
          client.agentGeneratePatch(
            sessionId,
            String(input.summary ?? "Generate the patch"),
          ),
        ),
    },
    {
      name: "run_security_review",
      title: "Request a security review",
      description:
        "Request a deterministic policy review of the patch. Not yet connected to the " +
        "policy engine: the request is recorded and started is false. Any verdict is " +
        "advisory and opens no gate.",
      inputSchema: NO_INPUT,
      execute: () => safely(() => client.agentSecurityReview(sessionId)),
    },
    {
      name: "run_validation",
      title: "Request validation",
      description:
        "Request that the generated tests run in the sandbox. Not yet connected to the " +
        "executor: the request is recorded and started is false. Writes nothing to the " +
        "repository.",
      inputSchema: NO_INPUT,
      execute: () => safely(() => client.agentRunValidation(sessionId)),
    },
    {
      name: "create_pull_request",
      title: "Request a pull request",
      description:
        "Request that MCPForge open a pull request with the approved patch, on an " +
        "mcpforge/* branch. The default branch is never written to." +
        NEEDS_APPROVAL,
      inputSchema: {
        type: "object",
        properties: {
          title: { type: "string" },
          body: { type: "string" },
        },
        required: ["title"],
        additionalProperties: false,
      },
      execute: (input) =>
        safely(() =>
          client.agentCreatePullRequest(
            sessionId,
            String(input.title ?? ""),
            String(input.body ?? ""),
          ),
        ),
    },
  ];
}
