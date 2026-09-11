"use client";

import { useCallback, useEffect, useState } from "react";
import { ApprovalCard } from "@/components/approval/approval-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  atLeast,
  describeFailure,
  parsePlan,
  parseWorkflows,
  stageStatuses,
  type PlanView,
  type StageStatus,
  type WorkflowView,
} from "@/components/pipeline/journey";
import { ToolPlanView } from "@/components/pipeline/tool-plan-view";
import { WorkflowSelector } from "@/components/pipeline/workflow-selector";
import type { ApiClient } from "@/lib/api/client";
import type { RunStateDto } from "@/lib/api/types";

/**
 * The product journey, from connection to the tool-plan decision — ticket T5a.
 *
 * `pipelineState()` is the single source of truth. Every action is followed by
 * a fresh read, and nothing on screen changes because a POST returned: a stage
 * renders as done only when the state says so.
 *
 * This panel never creates or decides an approval by itself. The plan step's
 * route opens the TOOL_PLAN approval; `ApprovalCard` records the developer's
 * click through `/api/approvals`. The run reaches `TOOL_PLAN_APPROVED` only
 * when generation consumes that stored decision (`POST /pipeline/patch`),
 * which is ticket T5b — so this panel shows the decision as stored and the
 * stage as not yet done.
 */

export type JourneyApi = Pick<
  ApiClient,
  | "pipelineState"
  | "pipelineConnect"
  | "pipelineAnalyze"
  | "pipelineSelectWorkflows"
  | "pipelinePlan"
  | "pipelineReject"
  | "agentWorkflows"
  | "agentPlan"
  | "decideApproval"
>;

const POLL_MS = 5000;

const STATUS_BADGE: Record<
  StageStatus,
  { tone: "success" | "accent" | "danger" | "neutral"; glyph: string; text: string }
> = {
  done: { tone: "success", glyph: "✓", text: "Done" },
  current: { tone: "accent", glyph: "→", text: "Current" },
  failed: { tone: "danger", glyph: "✕", text: "Failed" },
  not_yet: { tone: "neutral", glyph: "·", text: "Not yet" },
};

export function JourneyPanel({
  api,
  sessionId,
  onPlanApprovalChange,
}: {
  api: JourneyApi;
  sessionId: string;
  /** The id of the TOOL_PLAN approval this panel is showing, so it is not shown twice. */
  onPlanApprovalChange?: (approvalId: string | null) => void;
}) {
  const [run, setRun] = useState<RunStateDto | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowView[] | null>(null);
  const [workflowsHash, setWorkflowsHash] = useState<string | null>(null);
  const [plan, setPlan] = useState<PlanView | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    let state: RunStateDto;
    try {
      state = await api.pipelineState(sessionId);
    } catch (e) {
      // Nothing is shown rather than the last known state.
      setRun(null);
      setReadError(describeFailure("Reading the run state", e));
      return;
    }
    setRun(state);
    setReadError(null);

    try {
      if (atLeast(state.state, "WORKFLOW_SELECTION_PENDING")) {
        const artifact = await api.agentWorkflows(sessionId);
        setWorkflows(parseWorkflows(artifact));
        setWorkflowsHash(artifact.artifact_hash);
      } else {
        setWorkflows(null);
        setWorkflowsHash(null);
      }
      if (atLeast(state.state, "TOOL_PLAN_APPROVAL_PENDING")) {
        setPlan(parsePlan(await api.agentPlan(sessionId)));
      } else {
        setPlan(null);
      }
    } catch (e) {
      setWorkflows(null);
      setPlan(null);
      setReadError(describeFailure("Reading the stored results", e));
    }
  }, [api, sessionId]);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (!cancelled) await refresh();
    };
    void tick();
    const timer = setInterval(() => void tick(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [refresh]);

  const planApprovalId =
    run?.state === "TOOL_PLAN_APPROVAL_PENDING" && plan
      ? (run.pending_gate?.approval?.id ?? null)
      : null;
  useEffect(() => {
    onPlanApprovalChange?.(planApprovalId);
  }, [planApprovalId, onPlanApprovalChange]);

  /** Runs one step, then re-reads state whatever happened. */
  async function act(label: string, step: () => Promise<unknown>) {
    setRunning(label);
    setActionError(null);
    try {
      await step();
    } catch (e) {
      setActionError(describeFailure(label, e));
    } finally {
      setRunning(null);
      await refresh();
    }
  }

  const busy = running !== null;

  return (
    <section aria-labelledby="journey-heading" className="flex flex-col gap-4 p-4">
      <h2 id="journey-heading" className="text-base font-semibold text-text">
        Make this app WebMCP-ready
      </h2>

      {readError ? (
        <p role="alert" className="rounded-control bg-danger-subtle p-3 text-sm text-text">
          {readError}
        </p>
      ) : null}

      {run ? (
        <>
          <ol aria-label="Journey stages" className="flex flex-wrap gap-2">
            {stageStatuses(run.state, run.failure).map(({ stage, status }) => {
              const b = STATUS_BADGE[status];
              return (
                <li
                  key={stage.id}
                  aria-current={status === "current" || status === "failed" ? "step" : undefined}
                  className="flex items-center gap-1 text-xs text-text"
                >
                  <span>{stage.label}</span>
                  <Badge tone={b.tone} glyph={b.glyph}>
                    {b.text}
                  </Badge>
                </li>
              );
            })}
          </ol>
          <p className="font-mono text-xs text-subtle">run state {run.state}</p>

          {run.failure ? (
            <p role="alert" className="rounded-control bg-danger-subtle p-3 text-sm text-text">
              Failed: {run.failure}
            </p>
          ) : null}
          {actionError ? (
            <p role="alert" className="rounded-control bg-danger-subtle p-3 text-sm text-text">
              {actionError}
            </p>
          ) : null}
          {running ? (
            <p role="status" className="text-sm text-muted">
              {running}: waiting for the server. Nothing is marked done until it answers.
            </p>
          ) : null}

          <NextAction
            run={run}
            busy={busy}
            workflows={workflows}
            workflowsHash={workflowsHash}
            plan={plan}
            onConnect={() => act("Connecting", () => api.pipelineConnect(sessionId))}
            onAnalyze={() => act("Analysis", () => api.pipelineAnalyze(sessionId))}
            onSelect={(ids) =>
              act("Selecting workflows", () =>
                api.pipelineSelectWorkflows(sessionId, { workflow_ids: ids }),
              )
            }
            onPlan={() => act("Designing the tool plan", () => api.pipelinePlan(sessionId))}
            onDecide={async (approvalId, decision) => {
              // Throws to the card, which shows the failure. Only a click gets here.
              const decided = await api.decideApproval(approvalId, decision);
              if (decided.status === "REJECTED") {
                await act("Returning to workflow selection", () =>
                  api.pipelineReject(sessionId, decided.id),
                );
              } else {
                await refresh();
              }
            }}
            onConsumeRejection={(approvalId) =>
              act("Returning to workflow selection", () =>
                api.pipelineReject(sessionId, approvalId),
              )
            }
          />
        </>
      ) : readError ? null : (
        <p className="text-sm text-muted">Reading the run state.</p>
      )}
    </section>
  );
}

function NextAction({
  run,
  busy,
  workflows,
  workflowsHash,
  plan,
  onConnect,
  onAnalyze,
  onSelect,
  onPlan,
  onDecide,
  onConsumeRejection,
}: {
  run: RunStateDto;
  busy: boolean;
  workflows: WorkflowView[] | null;
  workflowsHash: string | null;
  plan: PlanView | null;
  onConnect: () => void;
  onAnalyze: () => void;
  onSelect: (ids: string[]) => void;
  onPlan: () => void;
  onDecide: (approvalId: string, decision: "APPROVED" | "REJECTED") => Promise<void>;
  onConsumeRejection: (approvalId: string) => void;
}) {
  switch (run.state) {
    case "PROJECT_CREATED":
      return (
        <Step text="Connect the selected repository so it can be analyzed.">
          <Button onClick={onConnect} disabled={busy}>
            Connect repository
          </Button>
        </Step>
      );
    case "ANALYSIS_PENDING":
    case "ANALYSIS_RUNNING":
      return (
        <Step
          text={
            run.state === "ANALYSIS_RUNNING"
              ? "Analysis started but has not finished. You can run it again."
              : "Analyze the repository to find its workflows."
          }
        >
          <Button onClick={onAnalyze} disabled={busy}>
            {run.state === "ANALYSIS_RUNNING" ? "Retry analysis" : "Analyze repository"}
          </Button>
        </Step>
      );
    case "WORKFLOW_SELECTION_PENDING":
      if (!workflows) return <Step text="No stored analysis could be read for this run." />;
      return (
        <WorkflowSelector
          key={workflowsHash ?? "none"}
          workflows={workflows}
          busy={busy}
          onSubmit={onSelect}
        />
      );
    case "WORKFLOWS_SELECTED":
    case "TOOL_PLAN_RUNNING":
      return (
        <Step
          text={
            run.state === "TOOL_PLAN_RUNNING"
              ? "Designing the tool plan started but has not finished. You can run it again."
              : "Design WebMCP tools for the selected workflows."
          }
        >
          <Button onClick={onPlan} disabled={busy}>
            {run.state === "TOOL_PLAN_RUNNING" ? "Retry tool plan" : "Design tool plan"}
          </Button>
        </Step>
      );
    case "TOOL_PLAN_APPROVAL_PENDING": {
      // The card is never shown without the plan it covers.
      if (!plan) return <Step text="No stored tool plan could be read for this run." />;
      const approval = run.pending_gate?.approval ?? null;
      return (
        <div className="flex flex-col gap-4">
          <ToolPlanView plan={plan} />
          {approval === null ? (
            <Step text="No approval is open for this version of the plan." />
          ) : (
            <>
              <ApprovalCard
                approval={approval}
                currentArtifactHash={plan.hash}
                onDecide={(decision) => onDecide(approval.id, decision)}
              />
              {approval.status === "APPROVED" ? (
                <Step text="Your approval is stored. The run moves to TOOL_PLAN_APPROVED when code generation consumes it, which this screen does not do yet." />
              ) : null}
              {approval.status === "REJECTED" ? (
                <Step text="You rejected this plan. The run returns to workflow selection once the rejection is applied.">
                  <Button
                    variant="secondary"
                    onClick={() => onConsumeRejection(approval.id)}
                    disabled={busy}
                  >
                    Return to workflow selection
                  </Button>
                </Step>
              ) : null}
            </>
          )}
        </div>
      );
    }
    default:
      if (atLeast(run.state, "TOOL_PLAN_APPROVED")) {
        return (
          <div className="flex flex-col gap-4">
            <Step text="The tool plan is approved. Generation onward is not on this screen yet." />
            {plan ? <ToolPlanView plan={plan} /> : null}
          </div>
        );
      }
      // REPOSITORY_CONNECTED, ANALYSIS_COMPLETE, TOOL_PLAN_READY: passed through
      // inside one server call; there is nothing for the developer to do here.
      return <Step text="The server is between steps. There is no action to take right now." />;
  }
}

function Step({ text, children }: { text: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-start gap-2">
      <p className="text-sm text-muted">{text}</p>
      {children}
    </div>
  );
}
