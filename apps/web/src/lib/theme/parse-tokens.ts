/**
 * Reads the theme tokens out of globals.css.
 *
 * The CSS is the source of truth — it is what actually ships. The contrast
 * tests parse it directly, so editing a colour in globals.css alone is enough
 * to fail a test if it becomes unreadable. `tokens.ts` remains a typed mirror
 * for application code, and a test asserts the two agree.
 */

export type ThemeName = "light" | "dark";

const BLOCK_START: Record<ThemeName, RegExp> = {
  light: /:root,\s*\n?\[data-theme="light"\]\s*\{/,
  dark: /\[data-theme="dark"\]\s*\{/,
};

/** Extracts `--name: #hex;` declarations from one theme block. */
export function parseThemeBlock(css: string, theme: ThemeName): Record<string, string> {
  const start = css.match(BLOCK_START[theme]);
  if (!start || start.index === undefined) {
    throw new Error(`Could not find the '${theme}' theme block in globals.css`);
  }
  const from = start.index + start[0].length;
  const end = css.indexOf("}", from);
  if (end === -1) throw new Error(`Unterminated '${theme}' theme block`);

  const body = css.slice(from, end);
  const out: Record<string, string> = {};
  for (const match of body.matchAll(/--([a-z-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) {
    out[match[1]!] = match[2]!.toLowerCase();
  }
  if (Object.keys(out).length === 0) {
    throw new Error(`No hex tokens parsed from the '${theme}' block — the parser is broken`);
  }
  return out;
}
