# MCPForge — Security & Access Model

**Status:** Phase 0 baseline. This document is binding. Code that contradicts it is a defect, not a variation.

MCPForge reads proprietary source code and produces code that AI agents will be able to invoke. Both halves are security-critical, so security is a product requirement here, not a hardening pass at the end.

---

## 1. Threat model

| # | Threat | Control |
|---|---|---|
| T1 | User source code leaks to Gemini or logs | Path policy + secret filter run before context construction (§4); prompts never log file bodies |
| T2 | A repository secret is embedded in a prompt or a generated file | Quarantine before indexing; generated-patch scan before PR (§4.4) |
| T3 | MCPForge writes to a user's default branch | Branch-only writer, repository-id assertion, no force push (§6) |
| T4 | Prompt injection from repository content or from an agent caller drives a privileged action | Model output is data, never authorization (§7); all gates are deterministic |
| T5 | Generated WebMCP tool exposes a destructive capability without approval | Risk classification + mandatory approval for WRITE/DESTRUCTIVE, enforced server-side (§8) |
| T6 | Analysis job escapes its sandbox or exfiltrates over the network | SecureExecutionProvider: path jail, non-root, no outbound network for analysis (§3) |
| T7 | False trust signalling — user believes work ran in a TEE when it did not | Trust level enum, set only by verified attestation (§2) |
| T8 | Credentials exposed to the browser | Hard tier boundary; only `NEXT_PUBLIC_*` reaches the client (§9) |
| T9 | Over-broad GitHub access | GitHub App, per-repository installation, short-lived tokens (§6) |
| T10 | An AI agent calling MCPForge's own WebMCP tools performs actions the human never approved | Same approval records as the UI; no agent-only path (§8.3) |

## 2. Confidential execution

**Production target:** Google Confidential Space, or an equivalent attested confidential-computing boundary, for repository analysis of private code.

**Phase 0–7 reality:** `DevelopmentSecureExecutor` — process isolation, ephemeral workspace, path jail, no outbound network for analysis commands, resource limits. This is real isolation, and it is *not* hardware-backed.

The distinction is represented as an enum, never a boolean:

```python
class TrustLevel(StrEnum):
    DEVELOPMENT_ISOLATION = "DEVELOPMENT_ISOLATION"
    HARDWARE_ATTESTED     = "HARDWARE_ATTESTED"
```

Rules:
- `HARDWARE_ATTESTED` may be assigned **only** by code that has fetched and cryptographically verified an attestation token against the expected workload identity and image digest.
- There is no configuration flag, no environment variable, and no test fixture that can set `HARDWARE_ATTESTED` without that verification path.
- The UI renders the enum. The strings shown to the user are **"Development Isolation"** and **"Hardware-backed Confidential Execution Verified"** — the second string exists in exactly one branch of one component, guarded by the enum.
- Writing "TEE VERIFIED", "attested", or a green shield for `DEVELOPMENT_ISOLATION` is a `FAIL` at review, unconditionally.

## 3. Sandbox rules for repository jobs

- Ephemeral workspace per run, destroyed on completion and on failure.
- Non-root execution.
- Filesystem access confined to the workspace path (path jail; symlinks resolved and rejected if they escape).
- No outbound network from analysis and validation commands. The clone step is the one networked operation and it targets only the bound repository host.
- CPU, memory, wall-clock and output-size limits; exceeded limits terminate the job with an explicit error.
- No arbitrary shell execution outside the sandbox. Commands are constructed from an allowlist with argument arrays — never string interpolation into a shell.

## 4. Secret protection

### 4.1 Ordering

Filtering happens **before** model context construction, before indexing, and before anything is persisted. There is no code path in which an unfiltered file body reaches a prompt builder.

### 4.2 Path-level exclusions (quarantine, never read)

```
.env, .env.*, *.pem, *.key, *.p8, *.p12, *.pfx, *.keystore, *.jks,
id_rsa*, id_dsa*, id_ecdsa*, id_ed25519*, *.crt, *.cer,
**/secrets/**, **/credentials/**, service-account*.json,
gcp-*.json, firebase-adminsdk-*.json, *.tfvars, .npmrc, .netrc,
.git-credentials, **/.aws/**, **/.ssh/**
```

### 4.3 Content-level scanning

Every file that survives path filtering is scanned before use for known credential formats — provider API key prefixes, PEM blocks, JWTs, connection strings with inline passwords, and high-entropy assignments to variables named like secrets. A hit quarantines the file and records a finding. Redaction of a partial match is not used for prompt safety: a file with a detected secret is excluded, not scrubbed and sent.

### 4.4 Outbound scan on generated content

The generated patch is scanned with the same detectors before the security review and again before PR creation. A hit blocks the PR.

### 4.5 Non-goals stated honestly

This is defence in depth, not a proof. A secret that looks like ordinary source text can survive filtering. The product says so in the Trust Panel rather than implying a guarantee.

## 5. Read-only by default

- A connected project begins in `READ_ONLY`.
- MCPForge cannot alter user code in this mode. There is no write code path reachable from a `READ_ONLY` project.
- Elevation to `WRITE_PR` requires: an explanation shown to the user of what will be written and where, an explicit user action, and a persisted record of who elevated and when.
- `WRITE_PR` permits exactly: create a branch, commit to that branch, open a PR. Nothing else.
- Every repository operation asserts the target repository id equals the project's bound repository id.
- A **demo project** (the bundled fixture application, `01_PRD.md` §7) has **no bound repository id**. It is therefore permanently ineligible for `WRITE_PR` and can reach no write path — the bound-id assertion above can never pass for it. This is enforced explicitly as well as structurally: elevation rejects a project without a bound repository id, and the PR writer refuses one. A demo project exists to exercise analysis, generation and validation; it never produces a pull request.

## 6. GitHub access

- GitHub App with per-repository installation. Never request account-wide repository access.
- The App private key is backend-only, loaded from the environment, never logged.
- Installation tokens are minted per operation and short-lived; they are never persisted.
- MCPForge user authentication is **separate** from GitHub repository authorization. Signing into MCPForge with GitHub grants identity, not repository access. Repository access requires the App installation flow. These are distinct records in the store and distinct checks in code.
- Branch naming: `mcpforge/webmcp-<project-or-workflow-slug>`.
- Never push to the default or any protected branch. Never force push. Never rewrite history. Never delete branches the user created.

## 7. Model output is never authorization

This is the rule most likely to be violated accidentally, so it is stated concretely.

- Every model response is parsed and validated against a Pydantic schema before any use. A validation failure is an error, not a warning.
- No branch in the codebase may read a boolean, a status, or an approval from model output and use it to permit a state transition. Approval comes from an `Approval` record in the store.
- The Human Approval Agent may map "yes, go ahead" to a *proposed* decision. Committing that decision is a deterministic function that requires an authenticated user id.
- The Security Reviewer agent's `PASS` is advisory input to a deterministic gate that also applies our own policy checks. An agent `PASS` cannot clear a policy violation found by code.
- Repository content is untrusted input. Instructions found inside analyzed source code are data. Prompts state this, and — more importantly — the architecture ensures a successful injection still cannot cross a gate, because gates do not read model output.

## 8. Generated-tool safety

### 8.1 Risk classification

Every proposed tool carries a risk class assigned by the Workflow Architect and **re-checked deterministically** against the mapped function's effects:

| Class | Meaning | Approval |
|---|---|---|
| `READ` | No state change | Not required |
| `WRITE` | Creates or modifies state | Required |
| `DESTRUCTIVE` | Deletes, cancels, charges, or is irreversible | Required, with explicit confirmation text |

If the agent's classification and the deterministic check disagree, the stricter one wins and the discrepancy is surfaced as a finding.

### 8.2 Generated code requirements

Generated tools must: validate input against their declared schema before executing; call existing application logic rather than reimplementing it; never accept a raw identifier that bypasses the application's own authorization; never take a parameter that selects a table, endpoint, path, or user id arbitrarily; return structured errors rather than raw exception text; and register with an abort signal so lifecycle teardown is clean.

Generated tools must not expose: authentication bypass, arbitrary redirects, raw SQL or query fragments, file path parameters, admin operations, or bulk destructive operations.

### 8.3 MCPForge's own tools

MCPForge's WebMCP surface follows the same rules it enforces on others. Read tools are open; anything mutating creates the same `Approval` record and returns "awaiting human approval". An agent cannot approve on the human's behalf, and there is no agent-only path around a gate.

## 9. Credential and tier boundaries

- Gemini API key: backend only. Never in a `NEXT_PUBLIC_*` variable, never proxied in a way that lets a client choose arbitrary prompts without our system instruction.
- GitHub App private key: backend only.
- Firebase Admin credentials: backend only. The Firebase **web** config is public by design and is the only Firebase material in the client.
- `.env` is never committed. `.env.example` contains variable names and non-secret defaults only.
- Backend verifies the Firebase ID token on every authenticated request; the client's claim of identity is never trusted.
- Logs redact tokens, keys and file bodies. Prompt/response retention for debugging is server-side, access-controlled, and excludes quarantined content.

## 10. Banned dependencies for V1

Unless a specific, written justification is added to this document:

- LangChain, LlamaIndex, CrewAI, AutoGen, or any heavy agent-orchestration framework
- `google-generativeai` (obsolete Gemini SDK) — use `google-genai`
- Arbitrary code-execution / `eval`-style libraries
- Any dependency that transmits source code to a third party
- Telemetry/analytics SDKs not required by a shipped feature
- Any "WebMCP shim" or polyfill presented as the official standard

## 11. Security review checklist (used at every gate)

1. No secret reachable by a prompt, a log, or the client bundle.
2. No write path to a default or protected branch; no force push.
3. Trust level correct and not overstated anywhere in code or UI.
4. Every state transition legal, persisted, and gated by a real `Approval` where required.
5. Every model output schema-validated before use.
6. No authorization decision derived from model output.
7. Generated tools correctly risk-classified and approval-gated.
8. Sandbox limits present on every executed command; no shell string interpolation.
9. No new banned dependency.
10. No mock or stub presented as real capability.
