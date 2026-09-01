"use client";

import { useEffect, useId, useState, type ReactNode } from "react";
import { Logo } from "@/components/layout/logo";
import { cn } from "@/lib/cn";

/**
 * The three-region workspace — 04_FRONTEND_SPEC.md §2 and §11.
 *
 * Desktop (>=1280px) shows all three regions. Tablet (768-1279px) collapses the
 * sidebar to an icon rail and moves the context panel into an overlay drawer, so
 * everything — approvals included — stays reachable. Mobile (<768px) shows the
 * workspace column, with both side regions available as drawers.
 */

export interface ShellRegion {
  /** Short label, used for the rail button and the drawer heading. */
  label: string;
  /** Single character or glyph shown on the icon rail. */
  glyph: string;
  content: ReactNode;
}

export function WorkspaceShell({
  sidebar,
  children,
  contextPanel,
}: {
  sidebar?: ShellRegion;
  children: ReactNode;
  contextPanel?: ShellRegion;
}) {
  const [openDrawer, setOpenDrawer] = useState<"sidebar" | "context" | null>(null);

  // Escape closes the drawer, and the drawer never traps the user.
  useEffect(() => {
    if (!openDrawer) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpenDrawer(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openDrawer]);

  return (
    <div className="flex min-h-screen bg-ground">
      {sidebar ? (
        <>
          {/* Icon rail: tablet and mobile */}
          <nav
            aria-label="Navigation rail"
            className="flex w-14 shrink-0 flex-col items-center border-r border-border bg-surface py-3 lg:hidden"
          >
            <Logo className="text-xs" />
            <RailButton
              label={sidebar.label}
              glyph={sidebar.glyph}
              onClick={() => setOpenDrawer("sidebar")}
            />
          </nav>

          {/* Full sidebar: desktop */}
          <aside
            aria-label={sidebar.label}
            className="hidden w-[260px] shrink-0 flex-col border-r border-border bg-surface lg:flex"
          >
            <div className="flex h-14 items-center border-b border-border px-4">
              <Logo />
            </div>
            <div className="flex-1 overflow-y-auto p-3">{sidebar.content}</div>
          </aside>
        </>
      ) : null}

      <main className="flex min-w-0 flex-1 flex-col">{children}</main>

      {contextPanel ? (
        <>
          {/* Drawer trigger below the desktop breakpoint */}
          <nav
            aria-label="Context rail"
            className="flex w-14 shrink-0 flex-col items-center border-l border-border bg-surface py-3 xl:hidden"
          >
            <RailButton
              label={contextPanel.label}
              glyph={contextPanel.glyph}
              onClick={() => setOpenDrawer("context")}
            />
          </nav>

          <aside
            aria-label={contextPanel.label}
            className="hidden w-[380px] shrink-0 flex-col border-l border-border bg-surface xl:flex"
          >
            {contextPanel.content}
          </aside>
        </>
      ) : null}

      {openDrawer ? (
        <Drawer
          region={openDrawer === "sidebar" ? sidebar! : contextPanel!}
          side={openDrawer === "sidebar" ? "left" : "right"}
          onClose={() => setOpenDrawer(null)}
        />
      ) : null}
    </div>
  );
}

function RailButton({
  label,
  glyph,
  onClick,
}: {
  label: string;
  glyph: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`Open ${label}`}
      className="mt-3 flex h-9 w-9 items-center justify-center rounded-control text-muted hover:bg-surface-sunken hover:text-text"
    >
      <span aria-hidden="true">{glyph}</span>
    </button>
  );
}

function Drawer({
  region,
  side,
  onClose,
}: {
  region: ShellRegion;
  side: "left" | "right";
  onClose: () => void;
}) {
  const headingId = useId();
  return (
    <div className="fixed inset-0 z-50 flex" role="presentation">
      <button
        type="button"
        aria-label="Close panel"
        onClick={onClose}
        className="absolute inset-0 bg-overlay"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        className={cn(
          "relative flex h-full w-[320px] flex-col border-border bg-surface",
          side === "left" ? "mr-auto border-r" : "ml-auto border-l",
        )}
      >
        <div className="flex h-14 items-center justify-between border-b border-border px-4">
          <h2 id={headingId} className="text-sm font-semibold text-text">
            {region.label}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-control px-2 py-1 text-sm text-muted hover:bg-surface-sunken hover:text-text"
          >
            Close
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-3">{region.content}</div>
      </div>
    </div>
  );
}
