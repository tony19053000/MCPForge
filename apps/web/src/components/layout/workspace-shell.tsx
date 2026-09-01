import type { ReactNode } from "react";
import { Logo } from "@/components/layout/logo";

/**
 * The three-region workspace — 04_FRONTEND_SPEC.md §2.
 *
 * Desktop is primary. On tablet the sidebar collapses to icons and the context
 * panel becomes a drawer; on mobile the workspace column stands alone. Phase 1
 * establishes the structure only — the panels are filled in later phases.
 */
export function WorkspaceShell({
  sidebar,
  children,
  contextPanel,
}: {
  sidebar?: ReactNode;
  children: ReactNode;
  contextPanel?: ReactNode;
}) {
  return (
    <div className="flex min-h-screen bg-ground">
      <aside
        aria-label="Projects and navigation"
        className="hidden w-[260px] shrink-0 flex-col border-r border-border bg-surface lg:flex"
      >
        <div className="flex h-14 items-center border-b border-border px-4">
          <Logo />
        </div>
        <div className="flex-1 overflow-y-auto p-3">{sidebar}</div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">{children}</main>

      {contextPanel ? (
        <aside
          aria-label="Context panel"
          className="hidden w-[380px] shrink-0 flex-col border-l border-border bg-surface xl:flex"
        >
          {contextPanel}
        </aside>
      ) : null}
    </div>
  );
}
