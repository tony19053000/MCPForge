import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  DiffView,
  collapseUnchanged,
  languageOf,
  parseDiff,
  type DiffFile,
} from "@/components/diff/diff-view";

function file(over: Partial<DiffFile> = {}): DiffFile {
  return {
    path: "src/webmcp/tools/searchRooms.ts",
    rationale: "Exposes searching rooms to AI agents by calling your existing searchRooms().",
    affectedTool: "search_rooms",
    diff: [
      "--- /dev/null",
      "+++ b/src/webmcp/tools/searchRooms.ts",
      "@@ -0,0 +1,3 @@",
      '+import { searchRooms as searchRoomsImpl } from "@/lib/rooms";',
      "+export async function searchRooms() {}",
    ].join("\n"),
    added: 2,
    removed: 0,
    ...over,
  };
}

describe("diff view", () => {
  it("shows the totals across all files", () => {
    render(<DiffView files={[file(), file({ path: "b.ts", added: 3, removed: 1 })]} />);
    // Scoped to the summary: the same counts also appear per file.
    const summary = within(screen.getByRole("banner", { name: "Change summary" }));
    expect(summary.getByText("2 files change")).toBeInTheDocument();
    expect(summary.getByText("+5")).toBeInTheDocument();
    expect(summary.getByText("−1")).toBeInTheDocument();
  });

  it("answers why each file changes", () => {
    // 04_FRONTEND_SPEC.md §6 — a reviewer should not have to infer this.
    render(<DiffView files={[file()]} />);
    expect(screen.getByText(/calling your existing searchRooms/)).toBeInTheDocument();
  });

  it("names the tool a file serves", () => {
    render(<DiffView files={[file()]} />);
    expect(screen.getByText("search_rooms")).toBeInTheDocument();
  });

  it("shows the path and language", () => {
    render(<DiffView files={[file()]} />);
    expect(
      screen.getByRole("button", { name: "src/webmcp/tools/searchRooms.ts" }),
    ).toBeInTheDocument();
    expect(screen.getByText("TypeScript")).toBeInTheDocument();
  });

  it("renders added lines", () => {
    render(<DiffView files={[file()]} />);
    expect(screen.getByText(/export async function searchRooms/)).toBeInTheDocument();
  });

  it("collapses files beyond the first few, and they open on demand", async () => {
    const many = Array.from({ length: 6 }, (_, i) => file({ path: `src/file${i}.ts` }));
    render(<DiffView files={many} />);

    // The fifth file's body is not rendered until asked for.
    const toggles = screen.getAllByRole("button", { name: /src\/file\d\.ts/ });
    expect(toggles[4]).toHaveAttribute("aria-expanded", "false");

    await userEvent.click(toggles[4]!);
    expect(toggles[4]).toHaveAttribute("aria-expanded", "true");
  });

  it("says so plainly when nothing changes", () => {
    render(<DiffView files={[]} />);
    expect(screen.getByText("No files change.")).toBeInTheDocument();
  });
});

describe("diff parsing", () => {
  it("classifies added, removed, context and metadata lines", () => {
    const lines = parseDiff("@@ -1 +1 @@\n-old\n+new\n unchanged");
    expect(lines.map((l) => l.kind)).toEqual(["meta", "remove", "add", "context"]);
  });

  it("keeps context around changes and collapses the rest", () => {
    const body = ["@@", ...Array.from({ length: 30 }, (_, i) => ` line ${i}`), "+changed"];
    const segments = collapseUnchanged(parseDiff(body.join("\n")));

    const collapsed = segments.filter((s) => s.kind === "collapsed");
    expect(collapsed.length).toBeGreaterThan(0);
    // Hidden lines are counted, never silently dropped.
    expect(collapsed[0]!.kind === "collapsed" && collapsed[0]!.count).toBeGreaterThan(0);
  });

  it("collapses nothing when every line is near a change", () => {
    const segments = collapseUnchanged(parseDiff("+a\n+b\n+c"));
    expect(segments.every((s) => s.kind === "lines")).toBe(true);
  });

  it("tells the reader how many lines are hidden", async () => {
    const body = ["@@", ...Array.from({ length: 40 }, (_, i) => ` line ${i}`), "+changed"];
    render(<DiffView files={[file({ diff: body.join("\n") })]} />);
    expect(screen.getByText(/unchanged lines/)).toBeInTheDocument();
  });
});

describe("language detection", () => {
  it.each([
    ["a.ts", "TypeScript"],
    ["a.tsx", "TypeScript"],
    ["a.json", "JSON"],
    ["a.css", "CSS"],
    ["Makefile", "MAKEFILE"],
  ])("%s is %s", (path, expected) => {
    expect(languageOf(path)).toBe(expected);
  });
});
