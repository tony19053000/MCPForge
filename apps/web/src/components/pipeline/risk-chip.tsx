import { Badge } from "@/components/ui/badge";
import type { Risk } from "@/components/pipeline/journey";

/** 04_FRONTEND_SPEC.md §7: text plus colour plus glyph, never colour alone. */
const RISK: Record<Risk, { tone: "neutral" | "warning" | "danger"; glyph: string; note: string }> =
  {
    READ: { tone: "neutral", glyph: "👁", note: "Safe" },
    WRITE: { tone: "warning", glyph: "✎", note: "Human approval" },
    DESTRUCTIVE: { tone: "danger", glyph: "⚠", note: "Human approval" },
  };

export function RiskChip({ risk }: { risk: Risk }) {
  const r = RISK[risk];
  return (
    <Badge tone={r.tone} glyph={r.glyph}>
      {risk} · {r.note}
    </Badge>
  );
}
