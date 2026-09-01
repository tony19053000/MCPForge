"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Per-region error boundary — 04_FRONTEND_SPEC.md §10.
 *
 * A failing context panel must not take down the session, so each region wraps
 * its own boundary. The real error is shown; never a generic message.
 */
export class RegionErrorBoundary extends Component<
  { region: string; children: ReactNode },
  { error: Error | null }
> {
  override state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`MCPForge region '${this.props.region}' failed`, error, info);
  }

  private reset = () => this.setState({ error: null });

  override render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div
        role="alert"
        className="m-3 rounded-card border border-danger bg-surface p-4"
      >
        <h2 className="text-sm font-semibold text-text">
          The {this.props.region} failed to render
        </h2>
        <p className="mt-1 text-sm text-muted">
          The rest of the session is unaffected.
        </p>
        <pre className="mt-3 overflow-x-auto rounded-control bg-surface-sunken p-2 font-mono text-xs text-text">
          {error.message}
        </pre>
        <button
          type="button"
          onClick={this.reset}
          className="mt-3 rounded-control border border-border-strong px-3 py-1.5 text-sm text-text hover:bg-surface-sunken"
        >
          Retry this panel
        </button>
      </div>
    );
  }
}
