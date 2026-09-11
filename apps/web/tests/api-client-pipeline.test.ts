/**
 * T4 — the pipeline client. Exact method, path and body per route, a bearer
 * token on every call, errors that keep their status and detail, and null
 * reads that stay null.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "@/lib/api/client";
import type { DiffFile } from "@/components/diff/diff-view";
import type { PatchFileDto } from "@/lib/api/types";

const BASE = "http://api.test";

function mockFetch(respond: () => Response = () => Response.json({ ok: true })) {
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
    respond(),
  );
  vi.stubGlobal("fetch", fetchMock);
  return { client: new ApiClient(async () => "test-token", BASE), fetchMock };
}

function lastCall(fetchMock: ReturnType<typeof mockFetch>["fetchMock"]) {
  const [url, init] = fetchMock.mock.calls.at(-1)!;
  return {
    url,
    method: init?.method ?? "GET",
    body: init?.body,
    auth: (init?.headers as Record<string, string>)["Authorization"],
  };
}

afterEach(() => vi.unstubAllGlobals());

const P = `${BASE}/api/sessions/s1/pipeline`;

describe("pipeline POST routes", () => {
  const cases: [string, (c: ApiClient) => Promise<unknown>, string, unknown][] = [
    ["connect (no body)", (c) => c.pipelineConnect("s1"), "/connect", undefined],
    [
      "connect (agent binding)",
      (c) => c.pipelineConnect("s1", { repository_binding_approval_id: "apr_1" }),
      "/connect",
      { repository_binding_approval_id: "apr_1" },
    ],
    ["analysis", (c) => c.pipelineAnalyze("s1"), "/analysis", undefined],
    [
      "workflows (selection)",
      (c) => c.pipelineSelectWorkflows("s1", { workflow_ids: ["w1", "w2"] }),
      "/workflows",
      { workflow_ids: ["w1", "w2"] },
    ],
    [
      "workflows (approved request)",
      (c) => c.pipelineSelectWorkflows("s1", { approval_id: "apr_2" }),
      "/workflows",
      { approval_id: "apr_2" },
    ],
    ["plan", (c) => c.pipelinePlan("s1"), "/plan", undefined],
    ["patch (retry)", (c) => c.pipelineGenerate("s1"), "/patch", undefined],
    [
      "patch (leave gate)",
      (c) => c.pipelineGenerate("s1", "apr_3"),
      "/patch",
      { approval_id: "apr_3" },
    ],
    ["security-review", (c) => c.pipelineSecurityReview("s1"), "/security-review", undefined],
    [
      "validation",
      (c) => c.pipelineValidate("s1", "apr_4"),
      "/validation",
      { approval_id: "apr_4" },
    ],
    [
      "pull-request/request",
      (c) => c.pipelineRequestPullRequest("s1"),
      "/pull-request/request",
      undefined,
    ],
    ["pull-request (retry)", (c) => c.pipelineCreatePullRequest("s1"), "/pull-request", undefined],
    [
      "pull-request (leave gate)",
      (c) => c.pipelineCreatePullRequest("s1", "apr_5"),
      "/pull-request",
      { approval_id: "apr_5" },
    ],
    ["reject", (c) => c.pipelineReject("s1", "apr_6"), "/reject", { approval_id: "apr_6" }],
  ];

  it.each(cases)("%s", async (_name, call, path, body) => {
    const { client, fetchMock } = mockFetch();
    await call(client);
    const sent = lastCall(fetchMock);
    expect(sent.url).toBe(`${P}${path}`);
    expect(sent.method).toBe("POST");
    expect(sent.auth).toBe("Bearer test-token");
    if (body === undefined) expect(sent.body).toBeUndefined();
    else expect(JSON.parse(sent.body as string)).toEqual(body);
  });

  it("returns the step response as sent, including a null approval", async () => {
    const step = {
      session_id: "s1",
      state: "ANALYSIS_COMPLETE",
      detail: "done",
      approval: null,
      pull_request_url: null,
    };
    const { client } = mockFetch(() => Response.json(step));
    await expect(client.pipelineAnalyze("s1")).resolves.toEqual(step);
  });
});

describe("pipeline reads", () => {
  const reads: [string, (c: ApiClient) => Promise<unknown>, string][] = [
    ["state", (c) => c.pipelineState("s1"), "/state"],
    ["patch", (c) => c.pipelinePatch("s1"), "/patch"],
    ["security-review", (c) => c.pipelineSecurityReviewResult("s1"), "/security-review"],
    ["validation", (c) => c.pipelineValidationResult("s1"), "/validation"],
    ["pull-request", (c) => c.pipelinePullRequest("s1"), "/pull-request"],
  ];

  it.each(reads)("%s is a GET with the token", async (_name, call, path) => {
    const { client, fetchMock } = mockFetch();
    await call(client);
    const sent = lastCall(fetchMock);
    expect(sent.url).toBe(`${P}${path}`);
    expect(sent.method).toBe("GET");
    expect(sent.body).toBeUndefined();
    expect(sent.auth).toBe("Bearer test-token");
  });

  it.each(reads.filter(([name]) => name !== "state"))(
    "%s: a stage that has not run stays null",
    async (_name, call) => {
      const { client } = mockFetch(() => Response.json(null));
      await expect(call(client)).resolves.toBeNull();
    },
  );

  it("a patch file fits DiffView's DiffFile props", () => {
    const file: PatchFileDto = {
      path: "a.ts",
      kind: "modify",
      rationale: "why",
      affectedTool: null,
      diff: "",
      added: 1,
      removed: 0,
    };
    const asDiff: DiffFile = file; // compile-time check
    expect(asDiff.affectedTool).toBeNull();
  });
});

describe("pipeline errors", () => {
  const statuses: [number, string, ApiError["kind"]][] = [
    [403, "Approval apr_1 is not approved", "approval_required"],
    [404, "Session not found", "not_found"],
    [409, "Illegal transition", "conflict"],
    [503, "No executor. Secure executor not attached: CONFIDENTIAL_SPACE_IMAGE unset", "unavailable"],
  ];

  it.each(statuses)("%i keeps status, detail and kind", async (code, detail, kind) => {
    const { client } = mockFetch(() => Response.json({ detail }, { status: code }));
    const error = await client.pipelinePlan("s1").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(code);
    expect(apiError.detail).toBe(detail);
    expect(apiError.kind).toBe(kind);
  });

  it("a failed read rejects rather than resolving to null", async () => {
    const { client } = mockFetch(() => Response.json({ detail: "boom" }, { status: 409 }));
    await expect(client.pipelinePatch("s1")).rejects.toBeInstanceOf(ApiError);
  });

  it("a non-JSON error body is the detail as-is", async () => {
    const { client } = mockFetch(() => new Response("upstream down", { status: 502 }));
    const error = (await client.pipelineState("s1").catch((e: unknown) => e)) as ApiError;
    expect(error.detail).toBe("upstream down");
    expect(error.kind).toBe("failed");
  });
});

describe("agent surface is unchanged", () => {
  it("agent routes keep their paths and methods", async () => {
    const { client, fetchMock } = mockFetch();
    await client.agentWorkflows("s1");
    expect(lastCall(fetchMock)).toMatchObject({
      url: `${BASE}/api/agent/sessions/s1/workflows`,
      method: "GET",
    });
    await client.agentPlan("s1");
    expect(lastCall(fetchMock).url).toBe(`${BASE}/api/agent/sessions/s1/plan`);
    await client.agentStartAnalysis("s1");
    expect(lastCall(fetchMock)).toMatchObject({
      url: `${BASE}/api/agent/sessions/s1/analysis`,
      method: "POST",
    });
    await client.agentGeneratePatch("s1", "sum");
    expect(JSON.parse(lastCall(fetchMock).body as string)).toEqual({ summary: "sum" });
  });

  it("ApiError.message is still the raw body the agent tools report", async () => {
    const { client } = mockFetch(() => Response.json({ detail: "no" }, { status: 403 }));
    const error = (await client.agentPlan("s1").catch((e: unknown) => e)) as ApiError;
    expect(error.message).toBe('{"detail":"no"}');
  });
});
