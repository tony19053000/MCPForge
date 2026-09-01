/**
 * WCAG AA is a requirement, not an aspiration — 04_FRONTEND_SPEC.md §1.
 *
 * These tests read globals.css directly, because that is what ships. A colour
 * cannot be changed to something unreadable without failing here.
 *
 * Body text needs 4.5:1. Large text and non-text UI boundaries need 3:1.
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import { contrastRatio, themes, type ThemeName } from "@/lib/theme/tokens";
import { parseThemeBlock } from "@/lib/theme/parse-tokens";

const CSS = readFileSync(path.resolve(__dirname, "../src/app/globals.css"), "utf8");
const AA_TEXT = 4.5;
const AA_LARGE = 3;
const themeNames: ThemeName[] = ["light", "dark"];

describe.each(themeNames)("%s theme, as shipped in globals.css", (name) => {
  const t = parseThemeBlock(CSS, name);
  const get = (token: string): string => {
    const value = t[token];
    if (!value) throw new Error(`Token --${token} is missing from the ${name} theme`);
    return value;
  };

  it.each([
    ["text on ground", "text", "ground"],
    ["text on surface", "text", "surface"],
    ["text on raised surface", "text", "surface-raised"],
    ["muted text on ground", "text-muted", "ground"],
    ["muted text on surface", "text-muted", "surface"],
  ])("%s meets AA for body text", (_label, fg, bg) => {
    expect(contrastRatio(get(fg), get(bg))).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it.each([
    ["accent on ground", "accent", "ground"],
    ["accent on surface", "accent", "surface"],
    ["success on surface", "success", "surface"],
    ["warning on surface", "warning", "surface"],
    ["danger on surface", "danger", "surface"],
    ["pending on surface", "pending", "surface"],
    ["subtle text on surface", "text-subtle", "surface"],
    ["focus ring on ground", "focus", "ground"],
    ["strong border on surface", "border-strong", "surface"],
  ])("%s meets AA for large text and UI boundaries", (_label, fg, bg) => {
    expect(contrastRatio(get(fg), get(bg))).toBeGreaterThanOrEqual(AA_LARGE);
  });

  it("accent text is readable on the accent fill", () => {
    expect(contrastRatio(get("accent-text"), get("accent"))).toBeGreaterThanOrEqual(AA_TEXT);
  });
});

describe("the typed token mirror", () => {
  it.each(themeNames)("matches globals.css for the %s theme", (name) => {
    const css = parseThemeBlock(CSS, name);
    const ts = themes[name];
    const pairs: Array<[keyof typeof ts, string]> = [
      ["ground", "ground"],
      ["surface", "surface"],
      ["surfaceRaised", "surface-raised"],
      ["border", "border"],
      ["borderStrong", "border-strong"],
      ["text", "text"],
      ["textMuted", "text-muted"],
      ["textSubtle", "text-subtle"],
      ["accent", "accent"],
      ["accentText", "accent-text"],
      ["success", "success"],
      ["warning", "warning"],
      ["danger", "danger"],
      ["pending", "pending"],
      ["focus", "focus"],
    ];
    for (const [tsKey, cssName] of pairs) {
      expect(ts[tsKey].toLowerCase(), `--${cssName} drifted from tokens.ts`).toBe(css[cssName]);
    }
  });
});

describe("the parser itself", () => {
  it("fails loudly rather than silently passing when a block is missing", () => {
    expect(() => parseThemeBlock("/* nothing here */", "dark")).toThrow(/Could not find/);
  });

  it("fails when a block contains no tokens, so an empty parse cannot pass", () => {
    expect(() => parseThemeBlock('[data-theme="dark"] { color-scheme: dark; }', "dark")).toThrow(
      /parser is broken/,
    );
  });
});

describe("reduced motion", () => {
  it("disables animation globally when the user asks for less motion", () => {
    // 04_FRONTEND_SPEC.md §1. Components use animate-pulse (streaming cursor,
    // skeletons); this rule is what makes that safe.
    const block = CSS.slice(CSS.indexOf("prefers-reduced-motion"));
    expect(block).toMatch(/animation-duration:\s*0\.01ms\s*!important/);
    expect(block).toMatch(/transition-duration:\s*0\.01ms\s*!important/);
    expect(block).toMatch(/scroll-behavior:\s*auto\s*!important/);
  });

  it("applies the rule to pseudo-elements too", () => {
    const block = CSS.slice(CSS.indexOf("prefers-reduced-motion"));
    expect(block).toContain("*::before");
    expect(block).toContain("*::after");
  });
});
