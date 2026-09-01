import { cn } from "@/lib/cn";

/** Matches final layout so nothing shifts when content arrives (spec §5). */
export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      role="status"
      aria-label="Loading"
      className={cn("animate-pulse rounded-[--radius-control] bg-surface-sunken", className)}
    />
  );
}
