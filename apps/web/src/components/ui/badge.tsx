import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * Meaning is never carried by colour alone (04_FRONTEND_SPEC.md §1), so every
 * badge renders a text label, and callers pass an icon or glyph where the
 * distinction matters at a glance.
 */
export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "pending";

const tones: Record<Tone, string> = {
  neutral: "bg-surface-sunken text-muted border-border",
  accent: "bg-accent-subtle text-accent border-accent",
  success: "bg-success-subtle text-success border-success",
  warning: "bg-warning-subtle text-warning border-warning",
  danger: "bg-danger-subtle text-danger border-danger",
  pending: "bg-pending-subtle text-pending border-pending",
};

export function Badge({
  tone = "neutral",
  glyph,
  children,
}: {
  tone?: Tone;
  glyph?: ReactNode;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5",
        "text-xs font-medium tracking-wide",
        tones[tone],
      )}
    >
      {glyph ? <span aria-hidden="true">{glyph}</span> : null}
      {children}
    </span>
  );
}
