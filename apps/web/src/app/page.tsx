import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Logo } from "@/components/layout/logo";
import { env } from "@/lib/env";

/**
 * Landing page.
 *
 * Every claim here describes something that exists or is explicitly labelled as
 * not yet built (01_PRD.md §11 — honest UI). Nothing implies a working pipeline
 * before one exists.
 */
export default function Home() {
  const authReady = env.enabledProviders.length > 0;

  return (
    <div className="min-h-screen bg-ground">
      <header className="border-b border-border">
        <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-6">
          <Logo className="text-base" />
          <Badge tone="accent">Phase 1 · foundation</Badge>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-20">
        <h1 className="max-w-2xl text-4xl font-semibold leading-tight tracking-tight text-text sm:text-5xl">
          Make your web app usable by AI agents — without guessing at the wiring.
        </h1>
        <p className="mt-6 max-w-2xl text-lg leading-relaxed text-muted">
          MCPForge reads a repository you control, finds the workflows worth exposing, designs
          WebMCP tools for them, generates the integration, reviews it for security, tests that an
          agent can actually use it, and opens a pull request. You approve every consequential
          step.
        </p>

        <div className="mt-10 flex flex-wrap items-center gap-3">
          {authReady ? (
            <Link
              href="/workspace"
              className="inline-flex h-10 items-center justify-center rounded-control bg-accent px-4 text-sm font-medium text-accent-text transition-colors hover:bg-accent-hover"
            >
              Open the workspace
            </Link>
          ) : (
            <Button disabled disabledReason="Sign-in is not configured for this deployment yet">
              Sign-in not configured
            </Button>
          )}
          <Link
            href="https://github.com/tony19053000/MCPForge"
            className="text-sm text-muted underline underline-offset-4 hover:text-text"
          >
            Read the architecture
          </Link>
        </div>

        <section className="mt-20 grid gap-4 sm:grid-cols-3">
          <Card>
            <h2 className="text-sm font-semibold text-text">Nothing leaves unfiltered</h2>
            <p className="mt-2 text-sm leading-relaxed text-muted">
              Secrets and build output are removed before your code is indexed, and long before any
              model sees a line of it.
            </p>
          </Card>
          <Card>
            <h2 className="text-sm font-semibold text-text">You hold every gate</h2>
            <p className="mt-2 text-sm leading-relaxed text-muted">
              Approvals are recorded state, not model output. An agent can drive MCPForge and still
              cannot approve anything on your behalf.
            </p>
          </Card>
          <Card>
            <h2 className="text-sm font-semibold text-text">Pull requests, never pushes</h2>
            <p className="mt-2 text-sm leading-relaxed text-muted">
              Access is read-only until you widen it, changes land on an <code>mcpforge/*</code>{" "}
              branch, and you merge.
            </p>
          </Card>
        </section>

        <section className="mt-16">
          <Card className="border-warning bg-warning-subtle">
            <h2 className="text-sm font-semibold text-text">
              Under construction — Phase 1 of 9
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-text">
              The application foundation is being built. Repository analysis, tool generation and
              pull-request creation are not implemented yet. Progress is tracked honestly in{" "}
              <code>STATUS.md</code>, which never reports a capability before it exists.
            </p>
          </Card>
        </section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto max-w-5xl px-6 py-8 text-sm text-subtle">
          MCPForge · MIT licensed · Supports Next.js, React and TypeScript applications
        </div>
      </footer>
    </div>
  );
}
