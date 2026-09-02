import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { SupportedFrameworks } from "@/components/supported-frameworks";

afterEach(() => vi.unstubAllGlobals());

function respondWith(body: unknown, ok = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status: ok ? 200 : 500 })),
  );
}

describe("supported frameworks", () => {
  it("names what the registry actually supports", async () => {
    // 01_PRD.md §9 — a table in the product, not a claim someone typed once.
    respondWith([{ framework: "next.js", display_name: "Next.js (App Router)" }]);
    render(await SupportedFrameworks());
    expect(screen.getByText("Supports Next.js (App Router)")).toBeInTheDocument();
  });

  it("lists several when several are registered", async () => {
    respondWith([
      { framework: "next.js", display_name: "Next.js (App Router)" },
      { framework: "vue", display_name: "Vue" },
    ]);
    render(await SupportedFrameworks());
    expect(screen.getByText("Supports Next.js (App Router), Vue")).toBeInTheDocument();
  });

  it("claims nothing when the API is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    render(await SupportedFrameworks());
    expect(screen.getByText("Supported frameworks unavailable")).toBeInTheDocument();
  });

  it("claims nothing when the API errors", async () => {
    respondWith([], false);
    render(await SupportedFrameworks());
    expect(screen.getByText("Supported frameworks unavailable")).toBeInTheDocument();
  });

  it("claims nothing when no adapter is registered", async () => {
    respondWith([]);
    render(await SupportedFrameworks());
    expect(screen.getByText("Supported frameworks unavailable")).toBeInTheDocument();
  });
});

describe("the landing page", () => {
  it("contains no hardcoded framework claim", async () => {
    const { readFileSync } = await import("node:fs");
    const path = await import("node:path");
    const source = readFileSync(
      path.resolve(__dirname, "../src/app/page.tsx"),
      "utf8",
    );
    expect(source).not.toMatch(/Supports Next\.js, React and TypeScript/);
  });
});
