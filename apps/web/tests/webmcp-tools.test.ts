import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "@/lib/api/client";
import { MockModelContext, createAdapter } from "@/webmcp/adapter";
import { registerMCPForgeTools } from "@/webmcp/register";
import { createTools, mutationTools, readTools, safely } from "@/webmcp/tools";

const SESSION = "sess_1";

function clientWith(handler: (path: string, init?: RequestInit) => unknown): ApiClient {
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    const result = handler(new URL(url, "http://api").pathname, init);
    if (result instanceof ApiError) {
      return new Response(result.message, { status: result.status });
    }
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  return new ApiClient(async () => "token", "http://api");
}

const AWAITING = {
  status: "awaiting_human_approval",
  approval_id: "appr_1",
  gate: "PATCH",
  artifact_hash: "abc123",
  message: "waiting for a human",
};

describe("tool schemas", () => {
  const tools = createTools(clientWith(() => ({})), SESSION);

  it("registers the tools the ticket names, and no others", () => {
    expect(tools.map((t) => t.name).sort()).toEqual(
      [
        "approve_webmcp_plan",
        "connect_project",
        "create_pull_request",
        "generate_patch",
        "get_project_status",
        "get_validation_report",
        "get_webmcp_plan",
        "list_detected_workflows",
        "run_security_review",
        "run_validation",
        "select_workflows",
        "start_repository_analysis",
      ].sort(),
    );
  });

  it("gives every tool a valid object schema", () => {
    for (const tool of tools) {
      expect(tool.inputSchema, tool.name).toBeDefined();
      const schema = tool.inputSchema as Record<string, unknown>;
      expect(schema.type, tool.name).toBe("object");
      expect(typeof schema.properties, tool.name).toBe("object");
      expect(schema.additionalProperties, tool.name).toBe(false);
    }
  });

  it("declares every required field as a real property", () => {
    for (const tool of tools) {
      const schema = tool.inputSchema as { properties: object; required?: string[] };
      for (const field of schema.required ?? []) {
        expect(Object.keys(schema.properties), `${tool.name}.${field}`).toContain(field);
      }
    }
  });

  it("gives every tool a non-trivial description", () => {
    for (const tool of tools) {
      expect(tool.description.length, tool.name).toBeGreaterThan(30);
    }
  });

  it("never takes a session id as tool input", () => {
    // An agent must not be able to point a tool at another session.
    for (const tool of tools) {
      const schema = tool.inputSchema as { properties: Record<string, unknown> };
      expect(Object.keys(schema.properties), tool.name).not.toContain("session_id");
    }
  });
});

/**
 * These tests prove what the client sends and how it handles what comes back.
 * They cannot prove the gate holds — the gate is server-side, and `fetch` is
 * stubbed here. `services/api/tests/test_agent_surface.py` is what tests the
 * gate against the real store; this file would pass unchanged if the server
 * approved everything, and is not evidence that it does not.
 */
describe("mutation tools cannot approve", () => {
  const names = mutationTools(clientWith(() => ({})), SESSION).map((t) => t.name);

  it("covers every mutation the ticket lists", () => {
    expect(names.sort()).toEqual(
      [
        "approve_webmcp_plan",
        "connect_project",
        "create_pull_request",
        "generate_patch",
        "run_security_review",
        "run_validation",
        "select_workflows",
      ].sort(),
    );
  });

  it("offers no field an agent could use to express a decision", () => {
    for (const tool of mutationTools(clientWith(() => ({})), SESSION)) {
      const schema = tool.inputSchema as { properties: Record<string, unknown> };
      for (const field of ["approved", "status", "decision", "actor_uid", "origin"]) {
        expect(Object.keys(schema.properties), `${tool.name}.${field}`).not.toContain(field);
      }
    }
  });

  it("says in the description that it only requests a decision", () => {
    // generate_patch is authorised by the approved tool plan and opens no gate
    // of its own, so it is not in this list.
    const gated = ["connect_project", "select_workflows", "create_pull_request"];
    for (const tool of mutationTools(clientWith(() => ({})), SESSION)) {
      if (gated.includes(tool.name)) {
        expect(tool.description, tool.name).toMatch(/cannot approve|asks the developer/i);
      }
    }
  });

  it("returns awaiting_human_approval rather than a success", async () => {
    const client = clientWith(() => AWAITING);
    const tool = mutationTools(client, SESSION).find((t) => t.name === "create_pull_request")!;
    const result = (await tool.execute({ title: "Add tools" })) as Record<string, unknown>;
    expect(result.status).toBe("awaiting_human_approval");
    expect(result.approval_id).toBe("appr_1");
  });

  it("posts to the agent surface, never to the decide endpoint", async () => {
    const seen: string[] = [];
    const client = clientWith((path) => {
      seen.push(path);
      return AWAITING;
    });
    for (const tool of mutationTools(client, SESSION)) {
      await tool.execute({ title: "t", branch: "main", repository_full_name: "a/b" });
    }
    expect(seen.length).toBeGreaterThan(0);
    for (const path of seen) {
      expect(path.startsWith("/api/agent/")).toBe(true);
      expect(path).not.toContain("decide");
    }
  });
});

describe("errors reach the agent as data, not exceptions", () => {
  it("maps an unauthenticated call to a structured error with no data", async () => {
    const client = clientWith(() => new ApiError(401, "Missing Authorization header"));
    const tool = readTools(client, SESSION)[0]!;
    const result = (await tool.execute({})) as Record<string, unknown>;
    expect(result.code).toBe("unauthenticated");
    expect(result.error).toContain("Authorization");
    expect(result).not.toHaveProperty("name");
  });

  it("distinguishes the failure kinds an agent should act on differently", async () => {
    for (const [status, code] of [
      [401, "unauthenticated"],
      [403, "forbidden"],
      [404, "not_found"],
      [409, "conflict"],
      [500, "failed"],
    ] as const) {
      const client = clientWith(() => new ApiError(status, "no"));
      const result = (await readTools(client, SESSION)[0]!.execute({})) as { code: string };
      expect(result.code, String(status)).toBe(code);
    }
  });

  it("does not leak an unexpected exception's detail", async () => {
    const result = (await safely(async () => {
      throw new Error("connection string postgres://user:pw@host");
    })) as { error: string };
    expect(result.error).not.toContain("postgres");
  });

  it("never rejects, whatever the tool does", async () => {
    for (const tool of createTools(
      clientWith(() => new ApiError(500, "boom")),
      SESSION,
    )) {
      await expect(tool.execute({})).resolves.toBeDefined();
    }
  });
});

describe("registration", () => {
  it("registers every tool against the adapter under one signal", async () => {
    const mock = new MockModelContext();
    const adapter = createAdapter({ useMock: true, mock });
    const controller = new AbortController();

    const result = await registerMCPForgeTools(
      adapter,
      clientWith(() => ({})),
      SESSION,
      controller.signal,
    );

    expect(result.tools).toHaveLength(12);
    expect(mock.tools).toHaveLength(12);
    expect(result.isMock).toBe(true);

    controller.abort();
    expect(mock.tools).toHaveLength(0);
  });

  it("reports honestly that nothing is registered on an unsupported browser", async () => {
    const result = await registerMCPForgeTools(
      createAdapter(),
      clientWith(() => ({})),
      SESSION,
      new AbortController().signal,
    );
    expect(result.supported).toBe(false);
    expect(result.isMock).toBe(false);
    expect(result.tools).toHaveLength(0);
  });

  it("an agent can drive a tool end to end through the mock", async () => {
    const mock = new MockModelContext();
    const adapter = createAdapter({ useMock: true, mock });
    await registerMCPForgeTools(
      adapter,
      clientWith(() => AWAITING),
      SESSION,
      new AbortController().signal,
    );

    const result = (await mock.call("generate_patch", { summary: "add tools" })) as {
      status: string;
    };
    expect(result.status).toBe("awaiting_human_approval");
  });
});

describe("tools do not advertise capability the code lacks", () => {
  const NOT_WIRED = ["start_repository_analysis", "generate_patch", "run_security_review", "run_validation"];

  it("says so in the description of every stage that is not connected", () => {
    for (const tool of createTools(clientWith(() => ({})), SESSION)) {
      if (NOT_WIRED.includes(tool.name)) {
        expect(tool.description, tool.name).toMatch(/not yet connected/i);
      }
    }
  });

  it("does not promise to run anything it does not run", () => {
    // The previous version of this test checked two literal prefixes, so a
    // description whose first sentence was exactly the false promise passed it.
    // Present-tense capability verbs are what an agent reads as a commitment.
    const PRESENT_TENSE = /\b(runs|starts|analyses|analyzes|executes|generates|returns pass)\b/i;
    for (const tool of createTools(clientWith(() => ({})), SESSION)) {
      if (NOT_WIRED.includes(tool.name)) {
        expect(tool.description, tool.name).not.toMatch(PRESENT_TENSE);
        expect(tool.description, tool.name).toMatch(/^Request /);
      }
    }
  });

  it("passes the server's started: false through rather than reporting success", async () => {
    const client = clientWith(() => ({
      session_id: SESSION,
      state: "PROJECT_CREATED",
      started: false,
      detail: "not yet connected to the orchestrator",
    }));
    for (const name of NOT_WIRED) {
      const tool = createTools(client, SESSION).find((t) => t.name === name)!;
      const result = (await tool.execute({})) as { started: boolean };
      expect(result.started, name).toBe(false);
    }
  });
});
