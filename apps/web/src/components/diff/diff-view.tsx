"use client";

import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/cn";

/**
 * Code diff view — 04_FRONTEND_SPEC.md §6.
 *
 * Per file: path, language, added/removed counts, and two things the spec is
 * specific about — a plain-language reason the file changes, and a chip naming
 * the tool it serves. A developer approving a patch should not have to infer
 * either.
 *
 * Unchanged regions collapse, and files start collapsed beyond the first few,
 * so a large patch does not render thousands of rows at once.
 */

export interface DiffFile {
  path: string;
  /** One sentence: why does this file change? */
  rationale: string;
  affectedTool?: string | null;
  /** Unified diff text for this file. */
  diff: string;
  added: number;
  removed: number;
}

type LineKind = "add" | "remove" | "context" | "meta";

interface DiffLine {
  kind: LineKind;
  text: string;
}

/** Files rendered expanded on first paint. The rest open on demand. */
const EXPANDED_BY_DEFAULT = 3;

/** Context lines kept around a change before the middle is collapsed. */
const CONTEXT_LINES = 3;

export function languageOf(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  return (
    {
      ts: "TypeScript",
      tsx: "TypeScript",
      js: "JavaScript",
      jsx: "JavaScript",
      json: "JSON",
      css: "CSS",
      md: "Markdown",
    }[extension] ?? (extension ? extension.toUpperCase() : "Text")
  );
}

export function parseDiff(diff: string): DiffLine[] {
  return diff.split("\n").map((text) => {
    if (text.startsWith("+++") || text.startsWith("---") || text.startsWith("@@")) {
      return { kind: "meta", text };
    }
    if (text.startsWith("+")) return { kind: "add", text };
    if (text.startsWith("-")) return { kind: "remove", text };
    return { kind: "context", text };
  });
}

/**
 * Collapse long unchanged stretches, keeping context around every change.
 *
 * Returns segments so the caller can render a "N unchanged lines" marker rather
 * than silently hiding them — a diff that hides content without saying so is
 * worse than a long one.
 */
export function collapseUnchanged(
  lines: DiffLine[],
  context = CONTEXT_LINES,
): Array<{ kind: "lines"; lines: DiffLine[] } | { kind: "collapsed"; count: number }> {
  const keep = new Set<number>();
  lines.forEach((line, index) => {
    if (line.kind === "add" || line.kind === "remove" || line.kind === "meta") {
      for (let i = index - context; i <= index + context; i += 1) {
        if (i >= 0 && i < lines.length) keep.add(i);
      }
    }
  });

  const out: Array<{ kind: "lines"; lines: DiffLine[] } | { kind: "collapsed"; count: number }> =
    [];
  let buffer: DiffLine[] = [];
  let hidden = 0;

  const flushLines = () => {
    if (buffer.length) {
      out.push({ kind: "lines", lines: buffer });
      buffer = [];
    }
  };
  const flushHidden = () => {
    if (hidden) {
      out.push({ kind: "collapsed", count: hidden });
      hidden = 0;
    }
  };

  lines.forEach((line, index) => {
    if (keep.has(index)) {
      flushHidden();
      buffer.push(line);
    } else {
      flushLines();
      hidden += 1;
    }
  });
  flushLines();
  flushHidden();
  return out;
}

export function DiffView({ files }: { files: readonly DiffFile[] }) {
  const totals = useMemo(
    () => ({
      added: files.reduce((sum, f) => sum + f.added, 0),
      removed: files.reduce((sum, f) => sum + f.removed, 0),
    }),
    [files],
  );

  if (files.length === 0) {
    return <p className="text-sm text-subtle">No files change.</p>;
  }

  return (
    <section aria-label="Proposed changes" className="flex flex-col gap-3">
      <header aria-label="Change summary" className="flex flex-wrap items-center gap-3 text-sm">
        <span className="text-text">
          {files.length} file{files.length === 1 ? "" : "s"} change
        </span>
        <span className="font-mono text-xs text-success">+{totals.added}</span>
        <span className="font-mono text-xs text-danger">−{totals.removed}</span>
      </header>

      {files.map((file, index) => (
        <FileDiff key={file.path} file={file} defaultOpen={index < EXPANDED_BY_DEFAULT} />
      ))}
    </section>
  );
}

function FileDiff({ file, defaultOpen }: { file: DiffFile; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const segments = useMemo(() => (open ? collapseUnchanged(parseDiff(file.diff)) : []), [
    open,
    file.diff,
  ]);

  return (
    <article className="overflow-hidden rounded-card border border-border bg-surface">
      <div className="flex flex-col gap-2 border-b border-border p-3">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="rounded-control font-mono text-sm text-text underline decoration-dotted underline-offset-4 hover:text-accent"
          >
            {file.path}
          </button>
          <span className="text-xs text-subtle">{languageOf(file.path)}</span>
          <span className="font-mono text-xs text-success">+{file.added}</span>
          <span className="font-mono text-xs text-danger">−{file.removed}</span>
          {file.affectedTool ? <Badge tone="accent">{file.affectedTool}</Badge> : null}
        </div>
        {/* §6: the diff must answer "why does this file change?" */}
        <p className="text-sm text-muted">{file.rationale}</p>
      </div>

      {open ? (
        <div className="overflow-x-auto">
          <pre className="min-w-full font-mono text-xs leading-relaxed">
            {segments.map((segment, i) =>
              segment.kind === "collapsed" ? (
                <div key={`gap-${i}`} className="bg-surface-sunken px-3 py-1 text-subtle">
                  {segment.count} unchanged line{segment.count === 1 ? "" : "s"}
                </div>
              ) : (
                segment.lines.map((line, j) => (
                  <div
                    key={`${i}-${j}`}
                    className={cn(
                      "px-3",
                      line.kind === "add" && "bg-success-subtle text-text",
                      line.kind === "remove" && "bg-danger-subtle text-text",
                      line.kind === "meta" && "text-subtle",
                    )}
                  >
                    {line.text || " "}
                  </div>
                ))
              ),
            )}
          </pre>
        </div>
      ) : null}
    </article>
  );
}
