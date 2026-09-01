/**
 * Tailwind v3 arbitrary-value syntax silently produces invalid CSS under v4.
 *
 * `bg-[--overlay]` compiles to `background-color:--overlay`, with no `var()`,
 * which the browser drops. Nothing fails: no build error, no lint error, no
 * type error — the style just does not apply. This has already slipped through
 * twice, so it is now a test.
 *
 * Use a theme token (`bg-overlay`, `rounded-card`) or the v4 shorthand
 * `bg-(--overlay)`.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "../src");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

/** Matches `something-[--token]`, the v3 form that breaks under v4. */
const V3_ARBITRARY_VAR = /[\w-]+-\[--[\w-]+\]/g;

describe("Tailwind v4 syntax", () => {
  const files = walk(SRC).filter((f) => /\.(tsx?|css)$/.test(f));

  it("finds source files to check, so an empty sweep cannot pass", () => {
    expect(files.length).toBeGreaterThan(5);
  });

  it("uses no v3 arbitrary-variable utilities", () => {
    const offenders: string[] = [];
    for (const file of files) {
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((line, i) => {
          for (const match of line.matchAll(V3_ARBITRARY_VAR)) {
            offenders.push(`${path.relative(SRC, file)}:${i + 1}: ${match[0]}`);
          }
        });
    }
    expect(offenders, "v3 syntax compiles to invalid CSS under Tailwind v4").toEqual([]);
  });

  it("detects the pattern it is meant to catch", () => {
    // Guards the regex itself, so the sweep above cannot pass by being broken.
    expect('className="bg-[--overlay]"'.match(V3_ARBITRARY_VAR)).toEqual(["bg-[--overlay]"]);
    expect('className="rounded-[--radius-card]"'.match(V3_ARBITRARY_VAR)).toEqual([
      "rounded-[--radius-card]",
    ]);
    expect('className="bg-overlay rounded-card bg-(--overlay)"'.match(V3_ARBITRARY_VAR)).toBeNull();
  });
});
