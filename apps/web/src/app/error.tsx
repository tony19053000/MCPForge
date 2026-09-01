"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";

/**
 * Error boundary — F1-07.
 *
 * Shows the real error and a recovery action. Never a generic
 * "something went wrong" (04_FRONTEND_SPEC.md §10).
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const router = useRouter();

  useEffect(() => {
    console.error("MCPForge render error", error);
  }, [error]);

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="w-full max-w-lg rounded-[--radius-card] border border-danger bg-surface p-6">
        <h1 className="text-lg font-semibold text-text">This view failed to render</h1>
        <p className="mt-2 text-sm text-muted">
          The rest of the application is unaffected. The error is shown in full so it can be
          reported or debugged.
        </p>
        <pre className="mt-4 overflow-x-auto rounded-[--radius-control] bg-surface-sunken p-3 font-mono text-xs text-text">
          {error.message}
          {error.digest ? `\n\ndigest: ${error.digest}` : ""}
        </pre>
        <div className="mt-5 flex gap-2">
          <Button onClick={reset}>Try again</Button>
          <Button variant="secondary" onClick={() => router.push("/")}>
            Back to start
          </Button>
        </div>
      </div>
    </main>
  );
}
