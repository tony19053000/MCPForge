/**
 * WCAG AA is a requirement, not an aspiration — 04_FRONTEND_SPEC.md §1.
 *
 * Body text needs 4.5:1. Large text and non-text UI boundaries need 3:1.
 */

import { describe, expect, it } from "vitest";
import { contrastRatio, themes, type ThemeName } from "@/lib/theme/tokens";

const AA_TEXT = 4.5;
const AA_LARGE = 3;

const themeNames: ThemeName[] = ["light", "dark"];

describe.each(themeNames)("%s theme contrast", (name) => {
  const t = themes[name];

  it.each([
    ["text on ground", t.text, t.ground],
    ["text on surface", t.text, t.surface],
    ["text on raised surface", t.text, t.surfaceRaised],
    ["muted text on ground", t.textMuted, t.ground],
    ["muted text on surface", t.textMuted, t.surface],
  ])("%s meets AA for body text", (_label, fg, bg) => {
    expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(AA_TEXT);
  });

  it.each([
    ["accent on ground", t.accent, t.ground],
    ["accent on surface", t.accent, t.surface],
    ["success on surface", t.success, t.surface],
    ["warning on surface", t.warning, t.surface],
    ["danger on surface", t.danger, t.surface],
    ["pending on surface", t.pending, t.surface],
    ["subtle text on surface", t.textSubtle, t.surface],
    ["focus ring on ground", t.focus, t.ground],
    ["strong border on surface", t.borderStrong, t.surface],
  ])("%s meets AA for large text and UI boundaries", (_label, fg, bg) => {
    expect(contrastRatio(fg, bg)).toBeGreaterThanOrEqual(AA_LARGE);
  });

  it("accent text is readable on the accent fill", () => {
    expect(contrastRatio(t.accentText, t.accent)).toBeGreaterThanOrEqual(AA_TEXT);
  });
});
