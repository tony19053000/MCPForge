# MCPForge — Project Status

> This file must always represent reality. Percentage advances only after `[REVIEWER / TESTER]` returns `PASS`.

---

## Overall completion

**0%** — Phase 0 authored, under review. The percentage advances to 10% only when `[REVIEWER / TESTER]` returns `PASS` and the work is committed.

## Current phase

**Phase 0 — Project Anchoring (0% → 10%)** — in review.

## Current ticket

`F0-05` — Phase 0 review gate and commit — `IN_REVIEW`

---

## Completed

None yet. No ticket is `DONE` until the review gate passes.

## In progress

| Ticket | Title | Status |
|---|---|---|
| F0-01 | Repository initialization and workspace strategy | `IN_REVIEW` |
| F0-02 | Claude Code development subagents | `IN_REVIEW` |
| F0-03 | Anchor documentation set | `IN_REVIEW` |
| F0-04 | CLAUDE.md and STATUS.md | `IN_REVIEW` |
| F0-05 | Phase 0 review gate and commit | `IN_REVIEW` |

## Pending

Phases 1–9, tickets `F1-01` through `F9-05`. See `05_FEATURE_TICKETS.md`.

**Phase plan:** ten phases (0–9), 10% each, summing to 100%. Phase 9 — Hardening, Demo and Launch — was added during the Phase 0 review after the reviewer found the original plan stopped at 90%.

---

## Blockers

| ID | Blocker | Impact | Status |
|---|---|---|---|
| B-01 | No Gemini API key configured | Phase 2 cannot run live model calls; provider and fake-provider tests can still be built | Open — needs owner to supply `GEMINI_API_KEY` |
| B-02 | No Firebase project configured | Phase 1 auth can be built against the abstraction; real sign-in needs a project | Open — needs owner |
| B-03 | No GitHub App registered | Phase 3 client can be built and unit-tested against a mocked API; real installation needs the App | Open — needs owner to register the App and supply id + private key |
| B-04 | No GCP Confidential Space infrastructure | Ticket `F8-02` cannot be completed and is marked `BLOCKED`. **It will not be simulated or marked done.** Development isolation continues to work and is labelled honestly | Open — expected; Phase 8 |

None of these block Phase 1. Work continues on everything that can be built and tested without them.

---

## Tests

| Check | State |
|---|---|
| Unit | Not applicable yet — no code |
| Integration | Not applicable yet |
| E2E | Not applicable yet |
| Build | Not applicable yet |
| Lint | Not applicable yet |
| Typecheck | Not applicable yet |

Phase 0 verification was a documentation consistency review, not a test run. No test command has been run because no code exists.

---

## Security state

| Control | State |
|---|---|
| Secure execution | Not implemented. Target for Phase 3 is `DEVELOPMENT_ISOLATION` |
| Attestation | **Not implemented, not simulated.** `HARDWARE_ATTESTED` is unreachable |
| Secret filtering | Specified (`03_SECURITY_ACCESS.md` §4), not implemented |
| Repository access mode | Not implemented. Default will be `READ_ONLY` |
| Branch protection | Not implemented. Writer will be branch + PR only |
| Credentials in repository | None. `.env` ignored; `.env.example` contains names only |
| Banned dependencies | None installed — no dependencies installed at all |

---

## Latest Git commit

**None.** The repository has no commits yet — Phase 0 is authored but uncommitted, pending the review gate. The first commit will be `docs: establish MCPForge product and engineering foundation`, and its SHA will be recorded here immediately afterwards.

---

## Context State Log

### 0001 — Phase 0: Project anchoring

**What was built.** The complete engineering and product foundation. No application code.

Files introduced:
- `.claude/agents/coder.md`, `.claude/agents/reviewer-tester.md` — the two development roles
- `01_PRD.md`, `02_ARCHITECTURE.md`, `03_SECURITY_ACCESS.md`, `04_FRONTEND_SPEC.md`, `05_FEATURE_TICKETS.md`
- `CLAUDE.md`, `STATUS.md`, `README.md`
- `.gitignore`, `.env.example`, root `package.json` (npm workspaces)

**Architecture decisions made.**

1. **Polyglot split: Python backend, TypeScript frontend.** The project owner specified a preference for a Python backend. This is also the right boundary: the backend's work is agent orchestration, structured-output validation and static analysis, where Pydantic v2 and the `google-genai` Python SDK fit best; the frontend must be TypeScript/React/Next.js regardless, because WebMCP is a browser API and the MVP's generation target is Next.js — MCPForge dogfoods what it generates. Duplicate type definitions are contained by generating the web tier's API types from the backend's OpenAPI schema. **Do not "unify" the stack to TypeScript.**
2. **Model id `gemini-3.7-flash` as the default**, verified against the official model documentation at Phase 0, and always read from `GEMINI_MODEL` — never a literal in application logic.
3. **`google-genai` only.** The older `google-generativeai` package is banned.
4. **WebMCP surface is `document.modelContext`**, per the W3C Web Machine Learning CG draft. Some third-party write-ups say `navigator.modelContext`; a few early implementations may expose it there. The adapter probes `document` first, then `navigator`, records which it found, and is the single file that knows the API shape — so spec drift is one file's problem. The draft has **no `unregisterTool`**; teardown is via the `AbortSignal` passed to `registerTool` options.
5. **No agent framework.** LangChain/CrewAI/AutoGen/LlamaIndex banned. Orchestration is a deterministic state machine we own.
6. **Repository understanding is deterministic first.** `Repository → index → relevant context → Gemini`. Baseline parsing is tree-sitter in Python (syntax, no type resolution). A `ts-morph` Node sidecar is a documented *possible future* addition if real type semantics prove necessary — it is not in MVP scope and must not be assumed to exist.
7. **npm workspaces + `uv`.** No Turborepo, no Nx — one JS package does not need a build orchestrator.
8. **Trust level is an enum, not a boolean.** `DEVELOPMENT_ISOLATION` | `HARDWARE_ATTESTED`, with `HARDWARE_ATTESTED` assignable only by verified attestation.

**Security decisions made.**
- Threat model T1–T10 written with a named control for each (`03_SECURITY_ACCESS.md` §1).
- Filtering ordering is binding: secret and path filtering happen **before** indexing and before prompt construction. A file with a detected secret is excluded, not scrubbed and sent.
- Approval records carry an artifact hash, so regenerating an artifact automatically invalidates its prior approval.
- An agent `PASS` is advisory only; a deterministic policy engine also runs and can override it. An agent verdict can never clear a policy violation found by code.
- MCPForge's own WebMCP mutation tools use the same approval records as the UI — there is no agent-only path around a gate.

**Unresolved issues.**
- Blockers B-01..B-04 above (no Gemini key, no Firebase project, no GitHub App, no Confidential Space infrastructure).
- WebMCP is a Community Group draft, not a standards-track spec, and is moving. The adapter isolates this risk; expect to revisit it at Phase 7.
- The exact Next.js and React major versions are chosen at `F1-01` against what is stable at that moment, then recorded here.

**What NOT to change accidentally.**
- The Python-backend / TypeScript-frontend split (decision 1).
- The filtering-before-prompt ordering.
- The rule that gates read `Approval` records and never model output.
- The trust-level enum and the single guarded UI branch that can render "Hardware-backed Confidential Execution Verified".
- The ban list in `03_SECURITY_ACCESS.md` §10.

**Review gate outcome.** The first reviewer pass returned `FAIL` with 13 defects. All were cleared before the gate was re-run. The substantive ones, recorded here because they shaped the documents:
- Phase 0 had been marked complete and committed when it was neither — tickets held at `IN_REVIEW` and the percentage held at 0% until an actual `PASS`.
- The phase plan summed to 90%. **Phase 9 — Hardening, Demo and Launch** was added with five tickets (F9-01..F9-05) covering end-to-end pipeline testing, performance, accessibility, open-source readiness and demo preparation.
- 33 of 45 tickets were missing required fields. Every ticket now carries all eight, including Files and Security on the attestation tickets.
- `04_FRONTEND_SPEC.md` offered "Upload project" and "Demo project" ingestion paths that existed in no other document. Resolved by adding a **bundled demo project** to MVP scope (ticket `F3-07`, a real fixture Next.js app that also serves every later phase's tests) and moving project upload to future scope.
- The readiness-score example totalled 96 while displaying 94 — in a document whose principle is that the total is computed from the rows. Corrected to 96.
- The reviewer agent claimed it could make small direct fixes while holding no edit tool. It is now strictly read-only; `Bash` is for running verification commands only.
- `.gitignore` was narrower than the security document's quarantine list (`.npmrc`, `*.tfvars`, `*.crt`, `**/.ssh/**` and others were not ignored). Now mirrors §4.2.
- `npm run build` was documented as fanning out to both stacks; Python has no build step. `CLAUDE.md` §8 now says so.
- `generation/` was referenced by ticket F5-04 but absent from the architecture's directory tree. Added.

**Next intended task.** `F1-01` — scaffold the Next.js application in `apps/web` with TypeScript strict and Tailwind, followed by `F1-02` (FastAPI service scaffold).
