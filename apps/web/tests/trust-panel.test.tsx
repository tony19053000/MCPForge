import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { TrustPanel } from "@/components/trust/trust-panel";
import type { SecureExecutionDto, TrustStateDto } from "@/lib/api/types";
import type { WebMCPState } from "@/webmcp/use-webmcp";

/**
 * The trust panel — F8-03, `04_FRONTEND_SPEC.md` §8.
 *
 * The panel's job is to be unflattering, so these tests are written against the
 * flattering failure in each row: a verified style for an unverified boundary,
 * a quarantine count for a scan that never ran, and a mock adapter presented as
 * browser support.
 *
 * The attested state below is fabricated by this test. Nothing in MCPForge can
 * produce it: `F8-02` is blocked, no code path obtains an attestation token,
 * and the backend sweep in `test_attestation.py` keeps that true.
 */

const DEVELOPMENT: SecureExecutionDto = {
  trust_level: "DEVELOPMENT_ISOLATION",
  configured_executor: "development",
  provider_running: true,
  evidence: null,
  detail: "The execution provider produced no attestation evidence.",
};

const ATTESTED: SecureExecutionDto = {
  trust_level: "HARDWARE_ATTESTED",
  configured_executor: "confidential_space",
  provider_running: true,
  evidence: {
    issuer: "https://confidentialcomputing.googleapis.com",
    audience: "mcpforge-run-1",
    subject: "instance",
    image_digest: `sha256:${"a".repeat(64)}`,
    image_reference: "us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor/x",
    workload_service_account: "mcpforge-workload@mcpforge-aa5c2.iam.gserviceaccount.com",
    hardware_model: "GCP_AMD_SEV_SNP",
    software_name: "CONFIDENTIAL_SPACE",
    debug_status: "disabled",
    issued_at: "2026-09-09T10:00:00+00:00",
    expires_at: "2026-09-09T11:00:00+00:00",
    verified_at: "2026-09-09T10:00:01+00:00",
  },
  detail: "Execution is backed by verified attestation evidence.",
};

function state(over: Partial<TrustStateDto> = {}): TrustStateDto {
  return {
    session_id: "sess_1",
    project_id: "proj_1",
    repository: {
      bound: true,
      repository_full_name: "acme/hotel-app",
      base_branch: "main",
      is_demo: false,
    },
    access_mode: "READ_ONLY",
    secret_filtering: {
      active: true,
      rule_count: 42,
      analyzed: true,
      quarantined_count: 3,
      quarantined_paths: [".env", "certs/server.pem", "src/config.ts"],
    },
    secure_execution: DEVELOPMENT,
    branch_protection: {
      branch_prefix: "mcpforge/",
      branch_shape: "^mcpforge/webmcp-[a-z0-9][a-z0-9-]{0,59}$",
      protected_names: ["main", "master"],
    },
    ...over,
  };
}

const SUPPORTED: WebMCPState = {
  surface: "document",
  supported: true,
  isMock: false,
  toolNames: ["mcpforge_get_project_status"],
};
const UNSUPPORTED: WebMCPState = {
  surface: "unsupported",
  supported: false,
  isMock: false,
  toolNames: [],
};
const MOCK: WebMCPState = { surface: "mock", supported: true, isMock: true, toolNames: [] };

/** The phrase `04_FRONTEND_SPEC.md` §8 reserves for a verified boundary. */
const VERIFIED_PHRASE = "Hardware-backed Confidential Execution Verified";

function panel(trust: TrustStateDto, webmcp: WebMCPState = SUPPORTED) {
  return render(<TrustPanel trust={trust} webmcp={webmcp} />);
}

describe("repository boundary and access mode", () => {
  it("shows the bound repository and its base branch", () => {
    panel(state());
    expect(screen.getByText("acme/hotel-app")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
  });

  it("says nothing is connected when nothing is", () => {
    panel(
      state({
        repository: {
          bound: false,
          repository_full_name: null,
          base_branch: null,
          is_demo: true,
        },
      }),
    );
    expect(screen.getByText(/No repository connected/)).toBeInTheDocument();
    expect(screen.queryByText("acme/hotel-app")).not.toBeInTheDocument();
  });

  it("renders the access mode the server reported, and warns when it is write", () => {
    panel(state());
    expect(screen.getByText("READ_ONLY")).toBeInTheDocument();
    expect(screen.queryByText(/pull request/)).not.toBeInTheDocument();

    panel(state({ access_mode: "WRITE_PR" }));
    expect(screen.getByText("WRITE_PR")).toBeInTheDocument();
    expect(screen.getByText(/can open a pull request/)).toBeInTheDocument();
  });
});

describe("secret filtering", () => {
  it("renders the server's count and lists the paths behind it", () => {
    panel(state());
    expect(screen.getByText(/3 files quarantined/)).toBeInTheDocument();
    expect(screen.getByText("certs/server.pem")).toBeInTheDocument();
  });

  it("counts what the server sent, not the length of any local list", () => {
    // A count of 1 with three paths would be a UI-side recount. The row states
    // the server's number.
    panel(state({ secret_filtering: { ...state().secret_filtering, quarantined_count: 1 } }));
    expect(screen.getByText(/1 file quarantined/)).toBeInTheDocument();
  });

  it("says nothing was analyzed rather than reporting zero", () => {
    panel(
      state({
        secret_filtering: {
          active: true,
          rule_count: 42,
          analyzed: false,
          quarantined_count: null,
          quarantined_paths: [],
        },
      }),
    );
    expect(screen.getByText(/nothing analyzed yet/)).toBeInTheDocument();
    expect(screen.queryByText(/quarantined/)).not.toBeInTheDocument();
  });

  it("reports inactive filtering as a warning rather than hiding it", () => {
    panel(
      state({
        secret_filtering: {
          active: false,
          rule_count: 0,
          analyzed: false,
          quarantined_count: null,
          quarantined_paths: [],
        },
      }),
    );
    expect(screen.getByText("Inactive")).toBeInTheDocument();
    expect(screen.getByText(/no filtering rules are loaded/)).toBeInTheDocument();
  });
});

describe("secure execution", () => {
  it("renders development isolation with the explicit unattested line", () => {
    panel(state());
    expect(screen.getByText("Development Isolation")).toBeInTheDocument();
    expect(screen.getByText("Not hardware-attested")).toBeInTheDocument();
    expect(screen.getByText(DEVELOPMENT.detail)).toBeInTheDocument();
    expect(screen.getByText("provider: development")).toBeInTheDocument();
  });

  it("never renders the verified phrase for DEVELOPMENT_ISOLATION", () => {
    panel(state());
    expect(screen.queryByText(VERIFIED_PHRASE)).not.toBeInTheDocument();
  });

  it("uses no verified styling anywhere while the boundary is unverified", () => {
    // "No green check for a state that has not been verified" — the rule is
    // about the whole panel, so this asserts on the rendered markup rather than
    // on one row: no success tone and no tick glyph exists at all.
    const { container } = panel(state({ access_mode: "WRITE_PR" }));
    expect(container.innerHTML).not.toContain("success");
    expect(container.innerHTML).not.toContain("✓");
  });

  it("still refuses the verified phrase when evidence is attached to an unverified level", () => {
    // Evidence alone is not the trigger. Only the enum is.
    panel(state({ secure_execution: { ...DEVELOPMENT, evidence: ATTESTED.evidence } }));
    expect(screen.queryByText(VERIFIED_PHRASE)).not.toBeInTheDocument();
    expect(screen.getByText("Not hardware-attested")).toBeInTheDocument();
  });

  it("says when no provider is running rather than implying one is", () => {
    panel(
      state({
        secure_execution: {
          ...DEVELOPMENT,
          provider_running: false,
          detail: "No execution provider is running in this API process, so no attestation exists.",
        },
      }),
    );
    expect(screen.getByText("provider: none running")).toBeInTheDocument();
    expect(screen.getByText(/No execution provider is running/)).toBeInTheDocument();
  });

  it("renders the verified branch only when the enum says HARDWARE_ATTESTED", () => {
    // The fabricated state above. This is the branch's only reachable input,
    // and the AST test in `trust-verified-branch.test.ts` keeps it the only one.
    const { container } = panel(state({ secure_execution: ATTESTED }));
    expect(screen.getByText(VERIFIED_PHRASE)).toBeInTheDocument();
    expect(screen.getByText("GCP_AMD_SEV_SNP")).toBeInTheDocument();
    expect(container.innerHTML).toContain("✓");
  });

  it("falls back to the raw level rather than borrowing a label it has no right to", () => {
    const unknown = { ...DEVELOPMENT, trust_level: "SOMETHING_ELSE" } as unknown as SecureExecutionDto;
    panel(state({ secure_execution: unknown }));
    expect(screen.getByText("SOMETHING_ELSE")).toBeInTheDocument();
    expect(screen.queryByText(VERIFIED_PHRASE)).not.toBeInTheDocument();
    expect(screen.getByText("Not hardware-attested")).toBeInTheDocument();
  });
});

describe("branch protection", () => {
  it("names the prefix the server said the writer enforces", () => {
    panel(state());
    expect(screen.getByText("mcpforge/*")).toBeInTheDocument();
    expect(screen.getByText(/never a force push/)).toBeInTheDocument();
  });

  it("uses the server's prefix rather than a copy of it", () => {
    panel(state({ branch_protection: { ...state().branch_protection, branch_prefix: "forge/" } }));
    expect(screen.getByText("forge/*")).toBeInTheDocument();
  });
});

describe("WebMCP adapter", () => {
  it("names the surface the real adapter found", () => {
    panel(state(), SUPPORTED);
    expect(screen.getByText("document.modelContext")).toBeInTheDocument();
    expect(screen.getByText(/supported/)).toBeInTheDocument();
  });

  it("reports the navigator surface when that is where it was found", () => {
    panel(state(), { ...SUPPORTED, surface: "navigator" });
    expect(screen.getByText("navigator.modelContext")).toBeInTheDocument();
  });

  it("says not supported in this browser when the API is absent", () => {
    panel(state(), UNSUPPORTED);
    expect(screen.getByText("not supported in this browser")).toBeInTheDocument();
    expect(screen.queryByText(/modelContext/)).not.toBeInTheDocument();
  });

  it("labels the mock adapter as a mock, in a warning style", () => {
    const { container } = panel(state(), MOCK);
    expect(screen.getByText("MOCK ADAPTER — not real browser WebMCP")).toBeInTheDocument();
    expect(container.innerHTML).toContain("text-warning");
    // A mock must never read as browser support.
    expect(screen.queryByText(/modelContext/)).not.toBeInTheDocument();
    expect(screen.queryByText("not supported in this browser")).not.toBeInTheDocument();
  });
});
