"use client";

import { useCallback, useEffect, useState } from "react";
import { ApprovalCard } from "@/components/approval/approval-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  atLeast,
  describeFailure,
  parsePatch,
  parsePlan,
  parsePullRequest,
  parseSecurityReview,
  parseValidation,
  parseWorkflows,
  stageStatuses,
  type PlanView,
  type StageStatus,
  type WorkflowView,
} from "@/components/pipeline/journey";
import { PatchView } from "@/components/pipeline/patch-view";
import { PullRequestView } from "@/components/pipeline/pull-request-view";
import { SecurityReviewView } from "@/components/pipeline/security-review-view";
import { ToolPlanView } from "@/components/pipeline/tool-plan-view";
import { ValidationView } from "@/components/pipeline/validation-view";
import { WorkflowSelector } from "@/components/pipeline/workflow-selector";
import type { ApiClient } from "@/lib/api/client";
import type {
  ApprovalDto,
  PatchDto,
  PullRequestDto,
  RunStateDto,
  SecurityReviewDto,
  ValidationDto,
} from "@/lib/api/types";

/**
 * The product journey, from connection to the pull request — tickets T5a, T5b.
 *
 * `pipelineState()` is the single source of truth. Every action is followed by
 * a fresh read, and nothing on screen changes because a POST returned: a stage
 * renders as done only when the state says so, and a stored result (patch,
 * review, validation, PR) is shown only as the server returns it.
 *
 * This panel never creates or decides an approval by itself. A stage's route
 * opens its gate; `ApprovalCard` records the developer's click through
 * `/api/approvals`, bound to the hash of the artifact shown beside it. A stored
 * APPROVED decision is consumed only by a second, explicit click that calls the
 * pipeline route which leaves the gate — and the server re-checks it there.
 */

export type JourneyApi = Pick<
  ApiClient,
  | "pipelineState"
  | "pipelineConnect"
  | "pipelineAnalyze"
  | "pipelineSelectWorkflows"
  | "pipelinePlan"
  | "pipelineReject"
  | "pipelineGenerate"
  | "pipelineSecurityReview"
  | "pipelineValidate"
  | "pipelineRequestPullRequest"
  | "pipelineCreatePullRequest"
  | "pipelinePatch"
  | "pipelineSecurityReviewResult"
  | "pipelineValidationResult"
  | "pipelinePullRequest"
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
  running: { tone: "accent", glyph: "◐", text: "Running" },
  current: { tone: "accent", glyph: "→", text: "Current" },
  failed: { tone: "danger", glyph: "✕", text: "Failed" },
  not_yet: { tone: "neutral", glyph: "·", text: "Not yet" },
};

/** What rejecting at each gate returns the run to, as the action label. */
const REJECTION_LABEL: Record<string, string> = {
  TOOL_PLAN: "Returning to workflow selection",
  PATCH: "Returning to the tool plan",
  PULL_REQUEST: "Returning to the patch decision",
};

interface Stored {
  workflows: WorkflowView[] | null;
  workflowsHash: string | null;
  plan: PlanView | null;
  patch: PatchDto | null;
  review: SecurityReviewDto | null;
  validation: ValidationDto | null;
  pr: PullRequestDto | null;
}

const NOTHING_STORED: Stored = {
  workflows: null,
  workflowsHash: null,
  plan: null,
  patch: null,
  review: null,
  validation: null,
  pr: null,
};

export function JourneyPanel({
  api,
  sessionId,
  onGateApprovalChange,
}: {
  api: JourneyApi;
  sessionId: string;
  /** The id of the approval this panel is showing, so it is not shown twice. */
  onGateApprovalChange?: (approvalId: string | null) => void;
}) {
  const [run, setRun] = useState<RunStateDto | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [stored, setStored] = useState<Stored>(NOTHING_STORED);
  const [storedReadable, setStoredReadable] = useState(true);
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

    const s = state.state;
    try {
      const next: Stored = { ...NOTHING_STORED };
      if (atLeast(s, "WORKFLOW_SELECTION_PENDING")) {
        const artifact = await api.agentWorkflows(sessionId);
        next.workflows = parseWorkflows(artifact);
        next.workflowsHash = artifact.artifact_hash;
      }
      if (atLeast(s, "TOOL_PLAN_APPROVAL_PENDING")) {
        next.plan = parsePlan(await api.agentPlan(sessionId));
      }
      if (atLeast(s, "PATCH_READY")) {
        next.patch = parsePatch(await api.pipelinePatch(sessionId));
      }
      // A review or validation is read only once the run has reached that
      // stage's verdict in its current pass. After a regeneration the store
      // still holds the previous pass's record, which is not this patch's.
      if (atLeast(s, "SECURITY_REVIEW_FAILED")) {
        next.review = parseSecurityReview(await api.pipelineSecurityReviewResult(sessionId));
      }
      if (atLeast(s, "VALIDATION_FAILED")) {
        next.validation = parseValidation(await api.pipelineValidationResult(sessionId));
      }
      if (atLeast(s, "PR_APPROVAL_PENDING")) {
        next.pr = parsePullRequest(await api.pipelinePullRequest(sessionId));
      }
      setStored(next);
      setStoredReadable(true);
    } catch (e) {
      setStored(NOTHING_STORED);
      setStoredReadable(false);
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

  const shownApprovalId = run ? shownGateApproval(run, stored)?.id ?? null : null;
  useEffect(() => {
    onGateApprovalChange?.(shownApprovalId);
  }, [shownApprovalId, onGateApprovalChange]);

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
                  aria-current={status === "done" || status === "not_yet" ? undefined : "step"}
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

          {storedReadable ? (
            <NextAction
              run={run}
              busy={busy}
              stored={stored}
              on={{
                connect: () => act("Connecting", () => api.pipelineConnect(sessionId)),
                analyze: () => act("Analysis", () => api.pipelineAnalyze(sessionId)),
                select: (ids) =>
                  act("Selecting workflows", () =>
                    api.pipelineSelectWorkflows(sessionId, { workflow_ids: ids }),
                  ),
                plan: () => act("Designing the tool plan", () => api.pipelinePlan(sessionId)),
                generate: (approvalId) =>
                  act("Generating code", () =>
                    approvalId === undefined
                      ? api.pipelineGenerate(sessionId)
                      : api.pipelineGenerate(sessionId, approvalId),
                  ),
                review: () =>
                  act("Running the security review", () => api.pipelineSecurityReview(sessionId)),
                validate: (approvalId) =>
                  act("Starting validation", () => api.pipelineValidate(sessionId, approvalId)),
                requestPr: () =>
                  act("Requesting the pull request", () =>
                    api.pipelineRequestPullRequest(sessionId),
                  ),
                createPr: (approvalId) =>
                  act("Opening the pull request", () =>
                    approvalId === undefined
                      ? api.pipelineCreatePullRequest(sessionId)
                      : api.pipelineCreatePullRequest(sessionId, approvalId),
                  ),
                decide: async (approval, decision) => {
                  // Throws to the card, which shows the failure. Only a click gets here.
                  const decided = await api.decideApproval(approval.id, decision);
                  if (decided.status === "REJECTED") {
                    await act(REJECTION_LABEL[decided.gate] ?? "Applying the rejection", () =>
                      api.pipelineReject(sessionId, decided.id),
                    );
                  } else {
                    await refresh();
                  }
                },
                consumeRejection: (approval) =>
                  act(REJECTION_LABEL[approval.gate] ?? "Applying the rejection", () =>
                    api.pipelineReject(sessionId, approval.id),
                  ),
              }}
            />
          ) : (
            <Step text="The stored results for this run could not be read, so no action is offered." />
          )}
        </>
      ) : readError ? null : (
        <p className="text-sm text-muted">Reading the run state.</p>
      )}
    </section>
  );
}

/** The gate approval this panel renders a card for — only beside its artifact. */
function shownGateApproval(run: RunStateDto, stored: Stored): ApprovalDto | null {
  const approval = run.pending_gate?.approval ?? null;
  if (approval === null) return null;
  if (run.state === "TOOL_PLAN_APPROVAL_PENDING") return stored.plan ? approval : null;
  if (run.state === "PATCH_APPROVAL_PENDING" || run.state === "PR_APPROVAL_PENDING") {
    return stored.patch ? approval : null;
  }
  return null;
}

interface Actions {
  connect: () => void;
  analyze: () => void;
  select: (ids: string[]) => void;
  plan: () => void;
  generate: (approvalId?: string) => void;
  review: () => void;
  validate: (approvalId: string) => void;
  requestPr: () => void;
  createPr: (approvalId?: string) => void;
  decide: (approval: ApprovalDto, decision: "APPROVED" | "REJECTED") => Promise<void>;
  consumeRejection: (approval: ApprovalDto) => void;
}

function NextAction({
  run,
  busy,
  stored,
  on,
}: {
  run: RunStateDto;
  busy: boolean;
  stored: Stored;
  on: Actions;
}) {
  const { workflows, workflowsHash, plan, patch, review, validation, pr } = stored;
  const approval = run.pending_gate?.approval ?? null;
  const noPatch = <Step text="No stored patch could be read for this run." />;

  switch (run.state) {
    case "PROJECT_CREATED":
      return (
        <Step text="Connect the selected repository so it can be analyzed.">
          <Button onClick={on.connect} disabled={busy}>
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
          <Button onClick={on.analyze} disabled={busy}>
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
          onSubmit={on.select}
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
          <Button onClick={on.plan} disabled={busy}>
            {run.state === "TOOL_PLAN_RUNNING" ? "Retry tool plan" : "Design tool plan"}
          </Button>
        </Step>
      );
    case "TOOL_PLAN_APPROVAL_PENDING":
      // The card is never shown without the plan it covers.
      if (!plan) return <Step text="No stored tool plan could be read for this run." />;
      return (
        <div className="flex flex-col gap-4">
          <ToolPlanView plan={plan} />
          <GateDecision
            approval={approval}
            artifactHash={plan.hash}
            artifactName="plan"
            busy={busy}
            on={on}
            approvedText="Your approval is stored. Generating code consumes it: the server re-checks it, moves the run to TOOL_PLAN_APPROVED and builds the patch."
            consumeLabel="Generate code"
            onConsume={(id) => on.generate(id)}
            rejectedText="You rejected this plan. The run returns to workflow selection once the rejection is applied."
            rejectLabel="Return to workflow selection"
          />
        </div>
      );
    case "GENERATION_RUNNING":
      return (
        <Step text="Generating code started but has not finished. You can run it again.">
          <Button onClick={() => on.generate()} disabled={busy}>
            Retry generation
          </Button>
        </Step>
      );
    case "PATCH_READY":
      if (!patch) return noPatch;
      return (
        <div className="flex flex-col gap-4">
          <Step text="Code is generated. Run the security review before anything else happens to it.">
            <Button onClick={on.review} disabled={busy}>
              Run security review
            </Button>
          </Step>
          <PatchView patch={patch} />
        </div>
      );
    case "SECURITY_REVIEW_RUNNING":
      return (
        <div className="flex flex-col gap-4">
          <Step text="The security review started and the server has recorded no verdict yet. This screen cannot restart it; it updates when the server records one." />
          {patch ? <PatchView patch={patch} /> : null}
        </div>
      );
    case "SECURITY_REVIEW_FAILED":
      return (
        <div className="flex flex-col gap-4">
          <SecurityReviewView review={review} />
          <Step text="The security review did not pass, so this patch cannot be approved. Regenerating builds a new patch, which is reviewed again. The server limits how many times.">
            <Button variant="secondary" onClick={() => on.generate()} disabled={busy}>
              Regenerate code
            </Button>
          </Step>
          {patch ? <PatchView patch={patch} /> : null}
        </div>
      );
    case "PATCH_APPROVAL_PENDING":
      if (!patch) return noPatch;
      return (
        <div className="flex flex-col gap-4">
          <SecurityReviewView review={review} />
          <PatchView patch={patch} />
          <GateDecision
            approval={approval}
            artifactHash={patch.artifact_hash}
            artifactName="patch"
            busy={busy}
            on={on}
            approvedText="Your approval of this patch is stored. Validation consumes it, applies the patch in an isolated workspace and runs the checks."
            consumeLabel="Run validation"
            onConsume={(id) => on.validate(id)}
            rejectedText="You rejected this patch. The run returns to the tool plan once the rejection is applied."
            rejectLabel="Return to the tool plan"
          />
        </div>
      );
    case "VALIDATION_RUNNING":
      return (
        <div className="flex flex-col gap-4">
          <Step text="Validation started and the server has recorded no result yet. This screen cannot restart it; it updates when the server records one." />
          <SecurityReviewView review={review} />
        </div>
      );
    case "VALIDATION_FAILED":
      return (
        <div className="flex flex-col gap-4">
          <ValidationView validation={validation} />
          <Step text="Validation did not pass, so no pull request can be requested. Regenerating builds a new patch, which is reviewed and validated again. The server limits how many times.">
            <Button variant="secondary" onClick={() => on.generate()} disabled={busy}>
              Regenerate code
            </Button>
          </Step>
          {patch ? <PatchView patch={patch} /> : null}
        </div>
      );
    case "VALIDATION_PASSED":
      return (
        <div className="flex flex-col gap-4">
          <ValidationView validation={validation} />
          <Step text="Ask to open a pull request. The server refuses unless this project has write access (WRITE_PR) to its bound repository, and says why.">
            <Button onClick={on.requestPr} disabled={busy}>
              Request pull request
            </Button>
          </Step>
        </div>
      );
    case "PR_APPROVAL_PENDING":
      if (!patch) return noPatch;
      return (
        <div className="flex flex-col gap-4">
          <ValidationView validation={validation} />
          <PatchView patch={patch} />
          <GateDecision
            approval={approval}
            artifactHash={patch.artifact_hash}
            artifactName="patch"
            busy={busy}
            on={on}
            approvedText="Your approval is stored. Opening the pull request consumes it; the server re-checks both approvals and write access before it writes a branch."
            consumeLabel="Open pull request"
            onConsume={(id) => on.createPr(id)}
            rejectedText="You rejected the pull request. The run returns to the patch decision once the rejection is applied."
            rejectLabel="Return to the patch decision"
          />
        </div>
      );
    case "PR_CREATING":
      return (
        <div className="flex flex-col gap-4">
          <PullRequestView pr={pr} />
          <Step
            text={
              run.failure
                ? "Opening the pull request failed. Retrying uses the approvals already stored; the server checks them again."
                : "Opening the pull request started and has not finished. You can try again."
            }
          >
            <Button variant="secondary" onClick={() => on.createPr()} disabled={busy}>
              Retry opening the pull request
            </Button>
          </Step>
        </div>
      );
    case "PR_CREATED":
    case "COMPLETE":
      return (
        <div className="flex flex-col gap-4">
          <PullRequestView pr={pr} />
          <ValidationView validation={validation} />
          <SecurityReviewView review={review} />
        </div>
      );
    default:
      // REPOSITORY_CONNECTED, ANALYSIS_COMPLETE, TOOL_PLAN_READY, TOOL_PLAN_APPROVED,
      // SECURITY_REVIEW_PASSED, PATCH_APPROVED, PR_APPROVED: passed through inside
      // one server call; there is nothing for the developer to do here.
      return <Step text="The server is between steps. There is no action to take right now." />;
  }
}

/**
 * One gate's decision: the card bound to the artifact on screen, then — only
 * once the server holds an APPROVED decision over that same artifact — the
 * explicit action that consumes it.
 */
function GateDecision({
  approval,
  artifactHash,
  artifactName,
  busy,
  on,
  approvedText,
  consumeLabel,
  onConsume,
  rejectedText,
  rejectLabel,
}: {
  approval: ApprovalDto | null;
  artifactHash: string;
  artifactName: string;
  busy: boolean;
  on: Actions;
  approvedText: string;
  consumeLabel: string;
  onConsume: (approvalId: string) => void;
  rejectedText: string;
  rejectLabel: string;
}) {
  if (approval === null) {
    return <Step text={`No approval is open for this version of the ${artifactName}.`} />;
  }
  const current = approval.artifact_hash === artifactHash;
  return (
    <>
      <ApprovalCard
        approval={approval}
        currentArtifactHash={artifactHash}
        onDecide={(decision) => on.decide(approval, decision)}
      />
      {approval.status === "APPROVED" && current ? (
        <Step text={approvedText}>
          <Button onClick={() => onConsume(approval.id)} disabled={busy}>
            {consumeLabel}
          </Button>
        </Step>
      ) : null}
      {approval.status === "APPROVED" && !current ? (
        <Step text={`This approval covers a different version of the ${artifactName}, so it cannot be used.`} />
      ) : null}
      {approval.status === "REJECTED" ? (
        <Step text={rejectedText}>
          <Button variant="secondary" onClick={() => on.consumeRejection(approval)} disabled={busy}>
            {rejectLabel}
          </Button>
        </Step>
      ) : null}
    </>
  );
}

function Step({ text, children }: { text: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-start gap-2">
      <p className="text-sm text-muted">{text}</p>
      {children}
    </div>
  );
}
