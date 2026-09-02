import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ELEVATION_REASON, RepositoryPanel } from "@/components/repo/repository-panel";
import type { AccessDto, ProjectDto, RepositoryDto } from "@/lib/api/types";

const PROJECT: ProjectDto = {
  id: "proj_1",
  name: "hotel app",
  access_mode: "READ_ONLY",
  repository_full_name: null,
  is_demo: false,
};

const REPOS: RepositoryDto[] = [
  { id: "1", full_name: "acme/site", default_branch: "main", private: false },
  { id: "2", full_name: "acme/private-thing", default_branch: "develop", private: true },
];

const UNBOUND: AccessDto = {
  project_id: "proj_1",
  access_mode: "READ_ONLY",
  repository_full_name: null,
  base_branch: null,
  elevated_by: null,
  elevated_at: null,
};

const BOUND: AccessDto = { ...UNBOUND, repository_full_name: "acme/site", base_branch: "main" };

const ELEVATED: AccessDto = {
  ...BOUND,
  access_mode: "WRITE_PR",
  elevated_by: "uid-owner",
  elevated_at: "2026-09-01T10:00:00Z",
};

function panel(over: Partial<React.ComponentProps<typeof RepositoryPanel>> = {}) {
  return (
    <RepositoryPanel
      project={PROJECT}
      repositories={REPOS}
      access={UNBOUND}
      onBind={vi.fn()}
      onElevate={vi.fn()}
      onRevoke={vi.fn()}
      {...over}
    />
  );
}

describe("choosing a repository", () => {
  it("offers only the repositories the server returned", () => {
    render(panel());
    const options = screen.getAllByRole("option").map((o) => o.textContent);
    expect(options).toContain("acme/site");
    expect(options).toContain("acme/private-thing (private)");
    expect(options).toHaveLength(3); // the two, plus the empty prompt
  });

  it("cannot connect until a repository and a branch are chosen", async () => {
    render(panel());
    expect(screen.getByRole("button", { name: /connect/i })).toBeDisabled();
  });

  it("defaults the branch to the repository's default", async () => {
    render(panel());
    await userEvent.selectOptions(screen.getByLabelText("Repository to connect"), "2");
    expect(screen.getByLabelText("Base branch")).toHaveValue("develop");
  });

  it("binds with the id, full name and branch the server needs", async () => {
    const onBind = vi.fn().mockResolvedValue(undefined);
    render(panel({ onBind }));
    await userEvent.selectOptions(screen.getByLabelText("Repository to connect"), "1");
    await userEvent.click(screen.getByRole("button", { name: /connect/i }));
    expect(onBind).toHaveBeenCalledWith("1", "acme/site", "main");
  });

  it("explains what to do when the App is installed nowhere", () => {
    render(panel({ repositories: [] }));
    expect(screen.getByText(/only sees repositories you have installed/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Repository to connect")).not.toBeInTheDocument();
  });
});

describe("a bound project", () => {
  it("shows what it is bound to", () => {
    render(panel({ access: BOUND }));
    expect(screen.getByText("acme/site")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
  });

  it("cannot be silently repointed", () => {
    render(panel({ access: BOUND }));
    expect(screen.queryByLabelText("Repository to connect")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect/i })).not.toBeInTheDocument();
  });
});

describe("access mode is always visible", () => {
  it("reads read-only before elevation", () => {
    render(panel({ access: BOUND }));
    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
  });

  it("reads write after elevation", () => {
    render(panel({ access: ELEVATED }));
    expect(screen.getByText(/write: branch \+ PR/i)).toBeInTheDocument();
  });

  it("states the mode even on an unbound project", () => {
    render(panel());
    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
  });
});

describe("elevation", () => {
  it("shows the reason with the offer, not hidden behind it", () => {
    render(panel({ access: BOUND }));
    expect(screen.getByText(ELEVATION_REASON)).toBeVisible();
  });

  it("states that the default branch is never written to", () => {
    render(panel({ access: BOUND }));
    expect(screen.getByText(/never write to your default branch/i)).toBeInTheDocument();
  });

  it("elevates when the developer asks", async () => {
    const onElevate = vi.fn().mockResolvedValue(undefined);
    render(panel({ access: BOUND, onElevate }));
    await userEvent.click(screen.getByRole("button", { name: /enable write access/i }));
    expect(onElevate).toHaveBeenCalledOnce();
  });

  it("is reversible, and says who elevated it and when", async () => {
    const onRevoke = vi.fn().mockResolvedValue(undefined);
    render(panel({ access: ELEVATED, onRevoke }));
    expect(screen.getByText(/uid-owner/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /revoke write access/i }));
    expect(onRevoke).toHaveBeenCalledOnce();
  });

  it("does not offer elevation again while already elevated", () => {
    render(panel({ access: ELEVATED }));
    expect(screen.queryByRole("button", { name: /enable write access/i })).not.toBeInTheDocument();
  });
});

describe("a demo project", () => {
  const DEMO = { ...PROJECT, is_demo: true };

  it("says it is a demo project", () => {
    render(panel({ project: DEMO, access: UNBOUND }));
    expect(screen.getByText(/this is a demo project/i)).toBeVisible();
  });

  it("is not offered a repository to connect", () => {
    render(panel({ project: DEMO, access: UNBOUND }));
    expect(screen.queryByLabelText("Repository to connect")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connect/i })).not.toBeInTheDocument();
  });

  it("cannot be elevated", () => {
    render(panel({ project: DEMO, access: UNBOUND }));
    expect(screen.queryByRole("button", { name: /enable write access/i })).not.toBeInTheDocument();
  });

  it("is distinguished from a real project that is merely unbound", () => {
    // The old test passed with is_demo flipped, because only the missing
    // repository was ever asserted. A real unbound project gets the chooser.
    render(panel({ project: { ...PROJECT, is_demo: false }, access: UNBOUND }));
    expect(screen.queryByText(/this is a demo project/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Repository to connect")).toBeInTheDocument();
  });
});
