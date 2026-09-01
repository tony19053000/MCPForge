"use client";

import { useEffect, useRef, useState } from "react";

import { ApprovalCard } from "@/components/approval/approval-card";
import { RegionErrorBoundary } from "@/components/error-boundary";
import { ProviderButtons } from "@/components/auth/provider-buttons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { WorkspaceShell } from "@/components/layout/workspace-shell";
import { Chat } from "@/components/workspace/chat";
import { useAuth } from "@/lib/auth/context";
import type { ApprovalDto, ProjectDto, SessionDto } from "@/lib/api/types";

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
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [session, api]);

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
            <div className="p-4">
              {approval ? (
                <ApprovalCard
                  approval={approval}
                  onDecide={async (decision) => {
                    setApproval(await api.decideApproval(approval.id, decision));
                  }}
                />
              ) : (
                <p className="text-sm text-subtle">
                  Nothing needs your decision right now. Repository analysis and tool plans arrive
                  in a later phase.
                </p>
              )}
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
          <Chat sessionId={chatSession.id} transport={api} />
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
