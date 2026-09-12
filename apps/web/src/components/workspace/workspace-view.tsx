"use client";

import { useEffect, useRef, useState } from "react";

import { ApprovalCard } from "@/components/approval/approval-card";
import { JourneyPanel } from "@/components/pipeline/journey-panel";
import { RepositoryPanel } from "@/components/repo/repository-panel";
import { TrustPanel } from "@/components/trust/trust-panel";
import { ActivityTimeline } from "@/components/workspace/activity-timeline";
import { WebMCPStatus } from "@/components/workspace/webmcp-status";
import { useWebMCP } from "@/webmcp/use-webmcp";
import { RegionErrorBoundary } from "@/components/error-boundary";
import { ProviderButtons } from "@/components/auth/provider-buttons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { WorkspaceShell } from "@/components/layout/workspace-shell";
import { Chat } from "@/components/workspace/chat";
import { useAuth } from "@/lib/auth/context";
import type {
  AccessDto,
  ApprovalDto,
  EventDto,
  ProjectDto,
  RepositoryDto,
  SessionDto,
  TrustStateDto,
} from "@/lib/api/types";

/**
 * The workspace — 04_FRONTEND_SPEC.md §2.
 *
 * Phase 2 mounts the conversation and the approval gate. Repository analysis,
 * tool plans and diffs arrive in later phases; nothing here pretends they exist.
 */
export function WorkspaceView() {
  const { session, ready, availableProviders, signIn, signOut, api } = useAuth();
  const [project, setProject] = useState<ProjectDto | null>(null);
  const [chatSession, setChatSession] = useState<SessionDto | null>(null);
  const [approval, setApproval] = useState<ApprovalDto | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [repositories, setRepositories] = useState<readonly RepositoryDto[]>([]);
  const [access, setAccess] = useState<AccessDto | null>(null);
  const [reposLoading, setReposLoading] = useState(true);
  const [events, setEvents] = useState<readonly EventDto[]>([]);
  // Server-read security state for the trust panel — F8-03. Null until the
  // first read succeeds: the panel is absent rather than guessed at.
  const [trust, setTrust] = useState<TrustStateDto | null>(null);
  // The approval the journey panel is showing beside what it covers (the tool
  // plan, or the patch for PATCH and PULL_REQUEST). The context panel does not
  // repeat it, so each decision is taken once, next to its artifact.
  const [journeyApprovalId, setJourneyApprovalId] = useState<string | null>(null);

  // Registers MCPForge's own WebMCP tools for this session, and tears them down
  // on unmount. Without this the tools exist but nothing can reach them.
  const webmcp = useWebMCP(api, chatSession?.id ?? null);
  const startedRef = useRef(false);

  // Opens the workspace once a session exists. No state is set synchronously
  // here — every update happens after an awaited call, so the effect cannot
  // cascade renders.
  useEffect(() => {
    if (!session || startedRef.current) return;
    startedRef.current = true;
    let cancelled = false;

    void (async () => {
      try {
        const existing = await api.listProjects();
        const chosen = existing[0] ?? (await api.createProject("My project"));
        const created = await api.createSession(chosen.id);
        if (cancelled) return;
        setProject(chosen);
        setChatSession(created);
        setAccess({
          project_id: chosen.id,
          access_mode: chosen.access_mode,
          repository_full_name: chosen.repository_full_name,
          base_branch: null,
          elevated_by: null,
          elevated_at: null,
        });

        // A deployment without GitHub configured is a normal state, not an
        // error: the panel says the App is installed nowhere and the rest of
        // the workspace keeps working.
        try {
          const repos = await api.listRepositories();
          if (!cancelled) setRepositories(repos);
        } catch {
          if (!cancelled) setRepositories([]);
        } finally {
          if (!cancelled) setReposLoading(false);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [session, api]);

  // The server timeline is the only place an agent's actions show up: an agent
  // calls the API directly, so nothing about its work passes through this tab.
  // Polling is how the developer sees it happen.
  useEffect(() => {
    const sessionId = chatSession?.id;
    if (!sessionId) return;
    let cancelled = false;

    const poll = async () => {
      // Re-read on every poll rather than once: access mode, the quarantine
      // record and the execution boundary can all change during a session, and
      // a stale trust panel is a flattering one. Kept in its own try so a
      // failure here cannot stop the timeline from updating, or the reverse.
      try {
        const state = await api.getTrust(sessionId);
        if (!cancelled) setTrust(state);
      } catch {
        // Nothing is shown rather than the last known answer.
        if (!cancelled) setTrust(null);
      }

      try {
        const latest = await api.listEvents(sessionId);
        if (cancelled) return;
        setEvents(latest);

        // An approval an agent opened arrives as an event, not as a response to
        // anything this tab did. Find the newest one still awaiting a decision.
        const requested = [...latest].reverse().find((e) => e.kind === "approval.requested");
        const id = requested?.detail?.approval_id;
        if (typeof id !== "string") return;
        const found = await api.getApproval(id);
        if (!cancelled && found.status === "PENDING") setApproval(found);
      } catch {
        // A failed poll is not worth interrupting the workspace for. The next
        // one recovers, and the error surface stays reserved for real failures.
      }
    };

    void poll();
    const timer = setInterval(() => void poll(), 4000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [api, chatSession?.id]);

  if (!ready && !session) {
    return <CenteredNotice title="Loading" body="Checking your sign-in state." />;
  }

  if (!session) {
    return (
      <CenteredNotice
        title="Sign in to MCPForge"
        body="MCPForge reads a repository you control. Nothing is analyzed until you connect one."
      >
        {availableProviders.length === 0 ? (
          <p className="text-sm text-muted">
            Sign-in is not configured for this deployment. No provider can be used yet.
          </p>
        ) : (
          <ProviderButtons
            enabled={availableProviders}
            onSelect={(p) => {
              void signIn(p).catch((e) => setError(e instanceof Error ? e.message : String(e)));
            }}
          />
        )}
        {error ? (
          <p role="alert" className="mt-4 text-sm text-danger">
            {error}
          </p>
        ) : null}
      </CenteredNotice>
    );
  }

  return (
    <WorkspaceShell
      sidebar={{
        label: "Projects",
        glyph: "▤",
        content: (
          <RegionErrorBoundary region="sidebar">
            <div className="flex flex-col gap-3">
              <p className="text-xs uppercase tracking-wide text-subtle">Signed in</p>
              <p className="truncate text-sm text-text">{session.email ?? session.subject}</p>
              {project ? (
                <div className="flex flex-col gap-2 rounded-control border border-border p-3">
                  <span className="text-sm text-text">{project.name}</span>
                  <Badge tone={project.access_mode === "READ_ONLY" ? "neutral" : "warning"}>
                    {project.access_mode}
                  </Badge>
                  {project.is_demo ? (
                    <span className="text-xs text-subtle">No repository connected yet</span>
                  ) : null}
                </div>
              ) : null}
              <Button variant="ghost" size="sm" onClick={() => void signOut()}>
                Sign out
              </Button>
            </div>
          </RegionErrorBoundary>
        ),
      }}
      contextPanel={{
        label: "Context panel",
        glyph: "◨",
        content: (
          <RegionErrorBoundary region="context panel">
            <div className="flex flex-col gap-6 p-4">
              <WebMCPStatus state={webmcp} />
              {trust ? <TrustPanel trust={trust} webmcp={webmcp} /> : null}
              {project && access ? (
                <RepositoryPanel
                  project={project}
                  repositories={repositories}
                  access={access}
                  loading={reposLoading}
                  onBind={async (repositoryId, fullName, baseBranch) => {
                    setAccess(
                      await api.bindRepository(project.id, repositoryId, fullName, baseBranch),
                    );
                  }}
                  onElevate={async () => setAccess(await api.elevateAccess(project.id))}
                  onRevoke={async () => setAccess(await api.revokeAccess(project.id))}
                />
              ) : null}
              {approval && approval.id === journeyApprovalId ? (
                <p className="text-sm text-subtle">
                  This decision is shown in the workspace, next to what it covers.
                </p>
              ) : approval ? (
                <ApprovalCard
                  approval={approval}
                  onDecide={async (decision) => {
                    setApproval(await api.decideApproval(approval.id, decision));
                  }}
                />
              ) : (
                <p className="text-sm text-subtle">Nothing needs your decision right now.</p>
              )}

              {events.length > 0 ? (
                <div>
                  <h2 className="mb-2 text-sm font-medium text-text">Activity</h2>
                  <ActivityTimeline events={[...events]} />
                </div>
              ) : null}
            </div>
          </RegionErrorBoundary>
        ),
      }}
    >
      <RegionErrorBoundary region="workspace">
        {error ? (
          <p role="alert" className="m-4 rounded-control bg-danger-subtle p-3 text-sm text-text">
            {error}
          </p>
        ) : null}
        {chatSession ? (
          <>
            <div className="max-h-[55vh] shrink-0 overflow-y-auto border-b border-border">
              <JourneyPanel
                api={api}
                sessionId={chatSession.id}
                onGateApprovalChange={setJourneyApprovalId}
              />
            </div>
            <Chat sessionId={chatSession.id} transport={api} />
          </>
        ) : (
          <CenteredNotice title="Starting a session" body="Setting up your workspace." />
        )}
      </RegionErrorBoundary>
    </WorkspaceShell>
  );
}

function CenteredNotice({
  title,
  body,
  children,
}: {
  title: string;
  body: string;
  children?: React.ReactNode;
}) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-ground p-6">
      <Card className="w-full max-w-md">
        <h1 className="text-lg font-semibold text-text">{title}</h1>
        <p className="mt-2 text-sm leading-relaxed text-muted">{body}</p>
        {children ? <div className="mt-5">{children}</div> : null}
      </Card>
    </main>
  );
}
