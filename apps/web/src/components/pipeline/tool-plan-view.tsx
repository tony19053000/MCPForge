import { Badge } from "@/components/ui/badge";
import type { PlanView } from "@/components/pipeline/journey";
import { RiskChip } from "@/components/pipeline/risk-chip";

/**
 * The tool plan as stored — ticket T5a.
 *
 * This is what the TOOL_PLAN approval covers, so it is rendered next to the
 * approval card. Types the binding could not type-check are flagged per tool
 * (`types_not_checked`), because approving is approving those too.
 */
export function ToolPlanView({ plan }: { plan: PlanView }) {
  const uncheckedTools = Object.keys(plan.typesNotChecked).filter(
    (name) => (plan.typesNotChecked[name] ?? []).length > 0,
  );

  return (
    <section aria-labelledby="tool-plan-heading" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="tool-plan-heading" className="text-sm font-medium text-text">
          Tool plan
        </h3>
        <span className="font-mono text-xs text-subtle">version {plan.hash.slice(0, 12)}</span>
      </div>

      {uncheckedTools.length > 0 ? (
        <p role="note" className="rounded-control bg-warning-subtle p-3 text-sm text-text">
          {uncheckedTools.length} tool{uncheckedTools.length === 1 ? " uses" : "s use"} types that
          were not type-checked against your code: {uncheckedTools.join(", ")}.
        </p>
      ) : null}

      {plan.tools.length === 0 ? (
        <p className="text-sm text-muted">The stored plan contains no tools.</p>
      ) : null}

      <ul className="flex flex-col gap-3">
        {plan.tools.map((tool) => {
          const unchecked = plan.typesNotChecked[tool.name] ?? [];
          return (
            <li
              key={tool.name}
              aria-labelledby={`tool-${tool.name}`}
              className="rounded-control border border-border p-3"
            >
              <div className="flex flex-wrap items-center gap-2">
                <h4 id={`tool-${tool.name}`} className="font-mono text-sm text-text">
                  {tool.name}
                </h4>
                <RiskChip risk={tool.risk} />
                {tool.approvalRequired ? (
                  <Badge tone="pending" glyph="⏸">
                    gated: asks a human before running
                  </Badge>
                ) : (
                  <Badge tone="neutral">not gated</Badge>
                )}
              </div>
              <p className="mt-1 text-sm text-muted">{tool.description}</p>
              <p className="mt-1 text-xs text-subtle">
                Calls <span className="font-mono">{tool.mapsToFunction}</span> · returns{" "}
                {tool.outputDescription}
              </p>

              {tool.parameters.length === 0 ? (
                <p className="mt-2 text-xs text-subtle">Takes no input.</p>
              ) : (
                <table className="mt-2 w-full text-left text-xs">
                  <caption className="sr-only">Input schema for {tool.name}</caption>
                  <thead className="text-subtle">
                    <tr>
                      <th scope="col">Input</th>
                      <th scope="col">Type</th>
                      <th scope="col">Required</th>
                      <th scope="col">Description</th>
                    </tr>
                  </thead>
                  <tbody className="text-text">
                    {tool.parameters.map((p) => (
                      <tr key={p.name}>
                        <td className="font-mono">{p.name}</td>
                        <td className="font-mono">{p.jsonType}</td>
                        <td>{p.required ? "yes" : "no"}</td>
                        <td className="text-muted">{p.description}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {unchecked.length > 0 ? (
                <div className="mt-2 rounded-control border border-warning p-2 text-xs text-text">
                  <Badge tone="warning" glyph="⚠">
                    not type-checked
                  </Badge>
                  <ul className="mt-1 font-mono">
                    {unchecked.map((t) => (
                      <li key={t}>{t}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>

      {plan.notes.length > 0 ? (
        <ul className="list-disc pl-5 text-xs text-muted">
          {plan.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
