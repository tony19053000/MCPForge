import { describe, expect, it } from "vitest";
import { ApiClient } from "@/lib/api/client";
import { MockModelContext, createAdapter } from "@/webmcp/adapter";
import { registerMCPForgeTools } from "@/webmcp/register";

/**
 * The two halves meeting — F7-02, F7-03.
 *
 * Every other test in this directory stubs `fetch`, and every API test drives
 * FastAPI's TestClient directly, so the web tools and the real server never
 * meet. The Phase 7 reviewer called that out: nothing proved a WebMCP tool call
 * reaches the running API at all.
 *
 * This file closes that. It registers the real tools through the real adapter
 * and drives them over real HTTP against a running server. It is not E2E in the
 * browser sense — that is Playwright, deferred to F9-03 — but it is the first
 * check where a tool invocation crosses the tier boundary.
 *
 * Locally it skips when no server is listening, so a developer running the unit
 * suite does not get a red bar for a server they did not start:
 *
 *     uv run --directory services/api python scripts/live_api.py   # port 8099
 *     npm run test --workspace=apps/web
 *
 * A check that can only skip is a check that can never go red, so CI does not
 * get that option. The web CI job starts `services/api/scripts/live_api.py`,
 * waits for `/healthz`, and sets MCPFORGE_LIVE_REQUIRED=1 — which turns "no
 * server" from a skip into a failure. If the server dies, fails to start, or
 * the tools stop reaching it, this suite goes red rather than quietly green.
 */
const API = process.env.MCPFORGE_LIVE_API ?? "http://127.0.0.1:8099";
const REQUIRED = process.env.MCPFORGE_LIVE_REQUIRED === "1";
const client = new ApiClient(async () => "uid-alice", API);

async function serverIsUp(): Promise<boolean> {
  try {
    const response = await fetch(`${API}/healthz`, {
      signal: AbortSignal.timeout(2000),
    });
    return response.ok;
  } catch {
    return false;
  }
}

const up = await serverIsUp();

if (!up && REQUIRED) {
  describe("agent drives MCPForge for real", () => {
    it("has a live API to drive", () => {
      expect.fail(
        `MCPFORGE_LIVE_REQUIRED=1, but nothing is listening at ${API}. ` +
          "Start services/api/scripts/live_api.py. This check is not allowed to skip here.",
      );
    });
  });
}

const live = up ? describe : describe.skip;

live("agent drives MCPForge for real", () => {
  it("registers tools and drives the gate end to end", async () => {
    // Human sets up the project.
    const project = await client.createProject("live hotel");
    const session = await client.createSession(project.id);

    // Agent side: register the real tools through the adapter.
    const mock = new MockModelContext();
    const adapter = createAdapter({ useMock: true, mock });
    const result = await registerMCPForgeTools(
      adapter, client, session.id, new AbortController().signal,
    );
    expect(result.tools).toHaveLength(12);
    console.log("  tools registered:", result.tools.length);

    // Agent calls a read tool.
    const status = (await mock.call("get_project_status")) as Record<string, unknown>;
    expect(status.name).toBe("live hotel");
    console.log("  get_project_status ->", status.name, status.access_mode);

    // Agent calls a mutation tool, claiming approval.
    const asked = (await mock.call("select_workflows", {
      workflow_ids: ["searchRooms"],
    })) as Record<string, string>;
    expect(asked.status).toBe("awaiting_human_approval");
    console.log("  select_workflows ->", asked.status);

    // The gate is shut.
    let gate = await client.checkGate(session.id, "WORKFLOW_SELECTION", asked.artifact_hash!);
    expect(gate.open).toBe(false);
    console.log("  gate before human decision:", gate.open);

    // Human decides.
    const decided = await client.decideApproval(asked.approval_id!, "APPROVED");
    expect(decided.actor_uid).toBe("uid-alice");

    gate = await client.checkGate(session.id, "WORKFLOW_SELECTION", asked.artifact_hash!);
    expect(gate.open).toBe(true);
    console.log("  gate after human decision:", gate.open, "by", decided.actor_uid);

    // Timeline separates agent from human.
    const events = await client.listEvents(session.id);
    const origins = events.map((e) => `${e.origin}:${e.kind}`);
    expect(origins).toContain("AGENT:approval.requested");
    expect(origins).toContain("HUMAN:approval.decided");
    console.log("  timeline:", origins.join(", "));
  });

  it("returns a clean error, not data, when unauthenticated", async () => {
    const anon = new ApiClient(async () => "not-a-token", API);
    const mock = new MockModelContext();
    const adapter = createAdapter({ useMock: true, mock });
    await registerMCPForgeTools(adapter, anon, "sess_x", new AbortController().signal);

    const out = (await mock.call("get_project_status")) as Record<string, string>;
    expect(out.code).toBe("unauthenticated");
    expect(out).not.toHaveProperty("name");
    console.log("  unauthenticated ->", out.code);
  });
});
