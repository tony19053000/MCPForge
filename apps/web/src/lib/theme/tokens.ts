/**
 * The token contrast table — 04_FRONTEND_SPEC.md §1.
 *
 * Mirrors globals.css. Every pairing used for text or interactive elements is
 * asserted against WCAG AA in contrast.test.ts, so a token cannot be changed to
 * something unreadable without a test failing.
 */

export type ThemeName = "light" | "dark";

export interface ThemeTokens {
  ground: string;
  surface: string;
  surfaceRaised: string;
  border: string;
  borderStrong: string;
  text: string;
  textMuted: string;
  textSubtle: string;
  accent: string;
  accentText: string;
  success: string;
  warning: string;
  danger: string;
  pending: string;
  focus: string;
}

export const themes: Record<ThemeName, ThemeTokens> = {
  light: {
    ground: "#faf9f7",
    surface: "#ffffff",
    surfaceRaised: "#ffffff",
    border: "#e0dcd6",
    borderStrong: "#8f8880",
    text: "#1c1917",
    textMuted: "#57534e",
    textSubtle: "#78716c",
    accent: "#b45309",
    accentText: "#ffffff",
    success: "#15803d",
    warning: "#a16207",
    danger: "#b91c1c",
    pending: "#6d28d9",
    focus: "#0369a1",
  },
  dark: {
    ground: "#121110",
    surface: "#1a1817",
    surfaceRaised: "#232120",
    border: "#322e2c",
    borderStrong: "#736c66",
    text: "#f5f3f1",
    textMuted: "#b3aca7",
    textSubtle: "#8a827d",
    accent: "#f59e0b",
    accentText: "#1c1917",
    success: "#4ade80",
    warning: "#fbbf24",
    danger: "#f87171",
    pending: "#c4b5fd",
    focus: "#7dd3fc",
  },
};

/** Relative luminance per WCAG 2.1. */
export function luminance(hex: string): number {
  const value = hex.replace("#", "");
  const channels = [0, 2, 4].map((i) => Number.parseInt(value.slice(i, i + 2), 16) / 255);
  const linear = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * linear[0]! + 0.7152 * linear[1]! + 0.0722 * linear[2]!;
}

export function contrastRatio(a: string, b: string): number {
  const [lighter, darker] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (lighter! + 0.05) / (darker! + 0.05);
}
