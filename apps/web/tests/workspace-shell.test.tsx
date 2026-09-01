import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { WorkspaceShell } from "@/components/layout/workspace-shell";

const sidebar = { label: "Projects", glyph: "P", content: <p>project list</p> };
const context = { label: "Context panel", glyph: "C", content: <p>repository explorer</p> };

function renderShell() {
  return render(
    <WorkspaceShell sidebar={sidebar} contextPanel={context}>
      <p>workspace column</p>
    </WorkspaceShell>,
  );
}

describe("workspace shell", () => {
  it("renders all three regions", () => {
    renderShell();
    expect(screen.getByRole("complementary", { name: "Projects" })).toBeInTheDocument();
    expect(screen.getByText("workspace column")).toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Context panel" })).toBeInTheDocument();
  });

  it("keeps both side regions reachable below the desktop breakpoint", () => {
    // 04_FRONTEND_SPEC.md §11 — tablet must remain fully usable, including approvals.
    renderShell();
    expect(screen.getByRole("button", { name: "Open Projects" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open Context panel" })).toBeInTheDocument();
  });

  it("opens the context panel in a modal drawer", async () => {
    renderShell();
    await userEvent.click(screen.getByRole("button", { name: "Open Context panel" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleName("Context panel");
  });

  it("closes the drawer with Escape, so it never traps the user", async () => {
    renderShell();
    await userEvent.click(screen.getByRole("button", { name: "Open Projects" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes the drawer with the close control", async () => {
    renderShell();
    await userEvent.click(screen.getByRole("button", { name: "Open Projects" }));
    await userEvent.click(screen.getByRole("button", { name: /^close$/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("omits a region entirely when it is not supplied", () => {
    render(
      <WorkspaceShell>
        <p>workspace only</p>
      </WorkspaceShell>,
    );
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.getByText("workspace only")).toBeInTheDocument();
  });
});
