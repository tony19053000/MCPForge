"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { AccessDto, ProjectDto, RepositoryDto } from "@/lib/api/types";

/**
 * Repository selector and access controls — F7-05.
 *
 * This panel never decides anything. It renders what the server says the access
 * mode is, and it calls the routes; `github/boundary.py` is what enforces the
 * rules (03_SECURITY_ACCESS.md §5). If this component were wrong in every
 * branch below, the boundary would still refuse — which is why the elevation
 * copy states a consequence rather than asking the developer to trust the UI.
 */

/** Shown before elevating, every time. Never collapsed behind a "don't show again". */
export const ELEVATION_REASON =
  "MCPForge needs write access to create a branch and open a pull request. " +
  "It will never write to your default branch and will never force push.";

export interface RepositoryPanelProps {
  project: ProjectDto;
  /** Installation-scoped repositories only. The server never returns more. */
  repositories: readonly RepositoryDto[];
  access: AccessDto;
  loading?: boolean;
  onBind: (repositoryId: string, fullName: string, baseBranch: string) => Promise<void>;
  onElevate: () => Promise<void>;
  onRevoke: () => Promise<void>;
}

export function RepositoryPanel({
  project,
  repositories,
  access,
  loading = false,
  onBind,
  onElevate,
  onRevoke,
}: RepositoryPanelProps) {
  const bound = access.repository_full_name !== null;
  const elevated = access.access_mode === "WRITE_PR";

  return (
    <section aria-labelledby="repo-panel-heading" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="repo-panel-heading" className="text-sm font-medium text-text">
          Repository access
        </h2>
        {/* Access mode is visible at all times, not only while elevated. */}
        <Badge tone={elevated ? "warning" : "neutral"} glyph={elevated ? "✎" : "👁"}>
          {elevated ? "write: branch + PR" : "read-only"}
        </Badge>
      </div>

      {bound ? (
        <BoundRepository access={access} />
      ) : project.is_demo ? (
        <DemoProjectNotice />
      ) : (
        <RepositoryChooser
          project={project}
          repositories={repositories}
          loading={loading}
          onBind={onBind}
        />
      )}

      <AccessControls
        project={project}
        bound={bound && !project.is_demo}
        elevated={elevated}
        access={access}
        onElevate={onElevate}
        onRevoke={onRevoke}
      />
    </section>
  );
}

/**
 * A bound project shows what it is bound to and offers no way to change it.
 * Repointing a project at a different repository silently is exactly the
 * mistake the boundary refuses, so the UI does not offer it either.
 */
function BoundRepository({ access }: { access: AccessDto }) {
  return (
    <div className="rounded-card border border-border bg-surface-sunken p-3">
      <p className="font-mono text-sm text-text">{access.repository_full_name}</p>
      <p className="mt-1 text-xs text-muted">
        base branch <span className="font-mono">{access.base_branch ?? "unknown"}</span>
      </p>
      <p className="mt-2 text-xs text-subtle">
        This project is bound to that repository. Binding cannot be changed from here — start a
        new project to analyse a different one.
      </p>
    </div>
  );
}

/**
 * A demo project runs against MCPForge's own fixture, not a repository the
 * developer owns. Offering it a repository chooser would imply it could be
 * bound to one, and the boundary would refuse.
 */
function DemoProjectNotice() {
  return (
    <div className="rounded-card border border-border bg-surface-sunken p-3">
      <p className="text-sm text-text">This is a demo project.</p>
      <p className="mt-1 text-xs text-muted">
        It has no repository, so nothing it produces can leave MCPForge. Create a new project to
        analyse a repository you own.
      </p>
    </div>
  );
}

function RepositoryChooser({
  project,
  repositories,
  loading,
  onBind,
}: {
  project: ProjectDto;
  repositories: readonly RepositoryDto[];
  loading: boolean;
  onBind: RepositoryPanelProps["onBind"];
}) {
  const [selectedId, setSelectedId] = useState("");
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState(false);

  const selected = repositories.find((r) => r.id === selectedId);
  const effectiveBranch = branch || selected?.default_branch || "";

  if (loading) {
    return <p className="text-sm text-muted">Loading your repositories…</p>;
  }

  if (repositories.length === 0) {
    return (
      <div className="rounded-card border border-border bg-surface-sunken p-3">
        <p className="text-sm text-text">No repositories available.</p>
        <p className="mt-1 text-xs text-muted">
          MCPForge only sees repositories you have installed its GitHub App on. Install it on the
          repository you want to analyse, then reload.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <label className="flex flex-col gap-1 text-xs text-muted">
        Repository
        <select
          aria-label="Repository to connect"
          value={selectedId}
          onChange={(e) => setSelectedId(e.target.value)}
          className="rounded-control border border-border bg-surface p-2 text-sm text-text"
        >
          <option value="">Choose a repository…</option>
          {repositories.map((repo) => (
            <option key={repo.id} value={repo.id}>
              {repo.full_name}
              {repo.private ? " (private)" : ""}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-xs text-muted">
        Base branch
        <input
          aria-label="Base branch"
          value={effectiveBranch}
          onChange={(e) => setBranch(e.target.value)}
          placeholder={selected?.default_branch ?? "main"}
          className="rounded-control border border-border bg-surface p-2 font-mono text-sm text-text"
        />
      </label>

      <Button
        variant="primary"
        disabled={!selected || !effectiveBranch || busy}
        disabledReason="Choose a repository and a base branch first"
        onClick={async () => {
          if (!selected) return;
          setBusy(true);
          try {
            await onBind(selected.id, selected.full_name, effectiveBranch);
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "Connecting…" : `Connect ${project.name}`}
      </Button>
    </div>
  );
}

function AccessControls({
  bound,
  elevated,
  access,
  onElevate,
  onRevoke,
}: {
  project: ProjectDto;
  bound: boolean;
  elevated: boolean;
  access: AccessDto;
  onElevate: () => Promise<void>;
  onRevoke: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);

  // A demo project has no repository, so there is nothing to elevate against.
  // Saying so is more useful than a disabled button with no explanation.
  if (!bound) {
    return (
      <p className="text-xs text-subtle">
        This project has no repository, so it cannot be given write access. Nothing it produces
        can leave MCPForge.
      </p>
    );
  }

  if (elevated) {
    return (
      <div className="rounded-card border border-warning bg-warning-subtle p-3">
        <p className="text-sm text-text">
          Write access is on. MCPForge can create a branch and open a pull request.
        </p>
        {access.elevated_by ? (
          <p className="mt-1 text-xs text-muted">
            Elevated by <span className="font-mono">{access.elevated_by}</span>
            {access.elevated_at ? ` on ${new Date(access.elevated_at).toLocaleString()}` : ""}
          </p>
        ) : null}
        <Button
          variant="secondary"
          className="mt-2"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await onRevoke();
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Revoking…" : "Revoke write access"}
        </Button>
      </div>
    );
  }

  return (
    <div className="rounded-card border border-border p-3">
      {/* The reason is shown with the offer, every time — 03_SECURITY_ACCESS.md §5. */}
      <p className="text-sm text-text">{ELEVATION_REASON}</p>
      <Button
        variant="secondary"
        className="mt-2"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await onElevate();
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy ? "Enabling…" : "Enable write access"}
      </Button>
    </div>
  );
}
