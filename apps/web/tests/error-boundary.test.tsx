import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { RegionErrorBoundary } from "@/components/error-boundary";

function Boom({ shouldThrow }: { shouldThrow: boolean }): React.ReactElement {
  if (shouldThrow) throw new Error("index pipeline exploded");
  return <p>panel content</p>;
}

describe("per-region error boundary", () => {
  beforeEach(() => vi.spyOn(console, "error").mockImplementation(() => {}));
  afterEach(() => vi.restoreAllMocks());

  it("renders children when nothing fails", () => {
    render(
      <RegionErrorBoundary region="context panel">
        <Boom shouldThrow={false} />
      </RegionErrorBoundary>,
    );
    expect(screen.getByText("panel content")).toBeInTheDocument();
  });

  it("shows the real error, not a generic message", () => {
    render(
      <RegionErrorBoundary region="context panel">
        <Boom shouldThrow />
      </RegionErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("index pipeline exploded")).toBeInTheDocument();
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
  });

  it("names the region that failed and says the session survives", () => {
    render(
      <RegionErrorBoundary region="context panel">
        <Boom shouldThrow />
      </RegionErrorBoundary>,
    );
    expect(screen.getByText(/context panel failed to render/i)).toBeInTheDocument();
    expect(screen.getByText(/rest of the session is unaffected/i)).toBeInTheDocument();
  });

  it("offers a recovery action that re-renders the region once the fault clears", async () => {
    // The child fails on first render and succeeds afterwards, so Retry is
    // exercised against a genuinely transient fault rather than a fresh mount.
    let failing = true;
    function Flaky() {
      if (failing) throw new Error("transient index failure");
      return <p>panel content</p>;
    }

    render(
      <RegionErrorBoundary region="context panel">
        <Flaky />
      </RegionErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    failing = false;
    await userEvent.click(screen.getByRole("button", { name: /retry this panel/i }));

    expect(screen.getByText("panel content")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("isolates failure to the region that threw", () => {
    render(
      <div>
        <RegionErrorBoundary region="context panel">
          <Boom shouldThrow />
        </RegionErrorBoundary>
        <p>workspace still here</p>
      </div>,
    );
    expect(screen.getByText("workspace still here")).toBeInTheDocument();
  });
});
