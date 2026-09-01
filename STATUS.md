# MCPForge — Project Status

> This file must always represent reality. Percentage advances only after `[REVIEWER / TESTER]` returns `PASS`.

---

## Overall completion

**20%** — Phase 1 complete, verified by `[REVIEWER / TESTER]` on round 3.

## Current phase

**Phase 2 — AI Workspace + Gemini (20% → 30%)** — not yet started.

## Current ticket

`F2-01` — Gemini provider — `PENDING`

---

## Completed

| Ticket | Title | Verified |
|---|---|---|
| F0-01 | Repository initialization and workspace strategy | PASS (round 4) |
| F0-02 | Claude Code development subagents | PASS (round 4) |
| F0-03 | Anchor documentation set | PASS (round 4) |
| F0-04 | CLAUDE.md and STATUS.md | PASS (round 4) |
| F0-05 | Phase 0 review gate and commit | PASS (round 4) |
| F1-01 | Next.js application scaffold | PASS (round 3) |
| F1-02 | FastAPI service scaffold | PASS (round 3) |
| F1-03 | Environment validation | PASS (round 3) |
| F1-04 | Design system foundation | PASS (round 3) |
| F1-05 | Application shell and landing page | PASS (round 3) |
| F1-06 | Auth abstraction and provisional Firebase wiring | PASS (round 3) |
| F1-07 | Error boundaries and CI baseline | PASS (round 3) |

## In progress

None.

## Pending

Phases 2–9, tickets `F2-01` through `F9-05`. See `05_FEATURE_TICKETS.md`.

**Phase plan:** ten phases (0–9), 10% each, summing to 100%. Phase 9 — Hardening, Demo and Launch — was added during the Phase 0 review after the reviewer found the original plan stopped at 90%.

---

## Blockers

| ID | Blocker | Impact | Status |
|---|---|---|---|
| B-01 | ~~No Gemini API key~~ — **resolved** | Key supplied and verified with a real structured call and a real stream against `gemini-3.7-flash`. Vertex/ADC is also implemented as a no-secret alternative | Closed |
| B-02 | ~~No Firebase project~~ — **resolved** | Firebase project created, Google sign-in enabled, ADC configured locally (quota project `launchforge-tee`) | Closed |
| B-05 | Service-account key downloads blocked by organization policy | No impact — the architecture was changed to need none. Token verification uses Google's public JWKS; other server-side Google access uses ADC | Closed by design change, not outstanding |
| B-03 | No GitHub App registered | Phase 3 client can be built and unit-tested against a mocked API; real installation needs the App | Open — needs owner to register the App and supply id + private key |
| B-04 | No GCP Confidential Space infrastructure | Ticket `F8-02` cannot be completed and is marked `BLOCKED`. **It will not be simulated or marked done.** Development isolation continues to work and is labelled honestly | Open — expected; Phase 8 |

None of these block Phase 1. Work continues on everything that can be built and tested without them.

---

## Tests

| Check | State |
|---|---|
| Unit | **122 passing** — 74 web (Vitest), 48 API (pytest) |
| Integration | Covered within the suites above: FastAPI routes exercised over ASGI transport with real RS256 tokens |
| E2E | Not started. Playwright is introduced at `F9-03`; there is deliberately no failing `test:e2e` script in the meantime |
| Build | `npm run build` clean (web only — Python has no build step) |
| Lint | `eslint` clean; `ruff check` and `ruff format --check` clean |
| Typecheck | `tsc --noEmit` clean under strict; `mypy` strict clean |

---

## Security state

| Control | State |
|---|---|
| Secure execution | Not implemented. Target for Phase 3 is `DEVELOPMENT_ISOLATION` |
| Client bundle | Verified free of `NEXT_PUBLIC_` values and any credential material |
| Auth enforcement | Server-side on every authenticated route; RS256 pinned; `alg:none`, expired, wrong-issuer, wrong-audience and forged tokens all rejected by test |
| Model tool invocation | SDK automatic function calling explicitly disabled. Every action is orchestrated by our code and gated by persisted approvals |
| Attestation | **Not implemented, not simulated.** `HARDWARE_ATTESTED` is unreachable |
| Secret filtering | Specified (`03_SECURITY_ACCESS.md` §4), not implemented |
| Repository access mode | Not implemented. Default will be `READ_ONLY` |
| Branch protection | Not implemented. Writer will be branch + PR only |
| Credentials in repository | None. `.env` ignored; `.env.example` contains names only |
| Service-account keys | None, by design. Not created, not committed, not a supported configuration. Server-side Google access uses ADC |
| Auth posture | Provisional Firebase Auth behind a `TokenVerifier` / `AuthProvider` port pair. Backend imports no Firebase SDK and needs no credentials to verify a token |
| Banned dependencies | None present. Verified absent: LangChain, LlamaIndex, CrewAI, AutoGen, `google-generativeai`. Also absent by design: `firebase-admin` |
| Attestation claims | `/healthz` reports `hardware_attested: false` as a literal with no assignment path |

---

## Latest Git commit

`7754003` — `fix: clear round-2 review findings`

Phase 1 spans `f744be7` (implementation), `16c8897` (round-1 fixes) and `7754003` (round-2 fixes), preceded by `06cb62b` (auth decision) and the Phase 0 commits `1430751` and `49e0162`.

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

**Review gate outcome.** `PASS` on the fourth reviewer round. Round 1 returned `FAIL` with 13 defects; round 2 confirmed those 13 fixed but found 2 residuals; round 3 confirmed those and found 1 knock-on contradiction introduced by the round-2 fix; round 4 passed. All were cleared before the gate closed. The substantive ones, recorded here because they shaped the documents:
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

### 0002 — Authentication decision revised before Phase 1

**Owner input.** A Firebase project was created with Google sign-in enabled, but **Firebase Auth is not committed to as the production solution** — the likely direction is direct Google OAuth, especially for a Vercel deployment. Service-account key creation is blocked by organization policy. Local Application Default Credentials are configured (`gcloud auth application-default login`, quota project `launchforge-tee`). Phase 1 must not block on final auth architecture, and authentication must stay cleanly removable.

**What changed, and why.**

1. **No `firebase-admin` dependency, and no service-account key.** A Firebase ID token is a standard RS256 JWT signed by Google with a public JWKS endpoint, so the backend verifies it directly with PyJWT — signature, `iss`, `aud`, `exp`, non-empty `sub`. This needs no credentials at all. It turns the organization-policy blocker into a non-issue rather than a workaround, removes a heavy dependency, and makes the eventual swap to Google OAuth a change of issuer, audience and JWKS URL in one adapter.
2. **Two ports, and no vendor type crosses either.** `TokenVerifier` → our `VerifiedIdentity` on the backend; `AuthProvider` → our `Session` on the web. The orchestrator, store and API layer never see a Firebase type. F1-06 now tests this directly: a test asserts `firebase` appears in no backend import, and a second `TokenVerifier` implementation proves the port is provider-agnostic.
3. **ADC for server-side Google credentials.** Development uses the local ADC file; production uses the deployment's workload identity. `GOOGLE_APPLICATION_CREDENTIALS` pointing at a key file is explicitly unsupported. `.env.example` was rewritten accordingly (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_QUOTA_PROJECT`).

**Documents updated.** `02_ARCHITECTURE.md` §3 table and new §3.2; `03_SECURITY_ACCESS.md` §9 credential rules and §6 auth-replaceability rule; `.env.example`; `05_FEATURE_TICKETS.md` F1-06; blockers B-02 (closed) and B-05 (closed by design change).

**What NOT to change accidentally.** The two ports, and the rule that no vendor identity type crosses them. The absence of any service-account key path. The fact that token verification is credential-free — if someone "simplifies" it back to `firebase-admin`, the organization-policy blocker returns and the swap cost goes up.

**Unresolved.** Final production auth (Firebase Auth vs direct Google OAuth) is deliberately undecided and is not a blocker. Deployment target is likely Vercel for the web tier; the Python service's hosting is not yet decided and is not needed until Phase 2.

**Next intended task.** `F1-01` — Next.js application scaffold.

### 0003 — Phase 1: Application foundation

**What was built.** Both tiers, with real gates and no stubbed AI.

- `apps/web` — Next.js 16 App Router, React 19, Tailwind v4, TypeScript strict (plus `noUncheckedIndexedAccess`). Design tokens for both themes, UI primitives, three-region workspace shell with an icon rail and modal drawers, landing page, pre-paint theme init, root and per-region error boundaries, `AuthProvider` port with a Firebase adapter, public env validation.
- `services/api` — Python 3.12, FastAPI, Pydantic v2, `uv`. Config that fails fast, `TokenVerifier` port with a credential-free Firebase JWKS verifier, structured logging with redaction, `/healthz` reporting real capability state.
- CI runs both stacks plus a credential scan and a check that `.env` is untracked.

**122 tests** — 74 web (Vitest/RTL), 48 API (pytest). typecheck, lint, test and build all clean.

**Decisions worth keeping.**

1. **Ports are enforced by test, not by convention.** The backend imports no vendor SDK and reads no key path; a second `TokenVerifier` and a second `AuthProvider` both satisfy their ports and the API works with one. Identity carries no repository authority, and a token claiming repository scopes grants none. These tests are the reason the eventual swap to direct Google OAuth stays a one-adapter change — do not delete them as redundant.
2. **The contrast test parses `globals.css` itself**, not a mirror, so a colour cannot become unreadable without failing. It caught a real defect during Phase 1: `--border-strong` failed 3:1 in both themes and was recomputed rather than waived.
3. **`css-syntax.test.ts` exists because one mistake shipped twice.** Tailwind v3's `x-[--token]` syntax compiles to invalid CSS under v4 with no build, lint or type error — the style simply does not apply. It reached the tree twice before becoming a test. Both that test and the contrast test guard themselves against passing vacuously.
4. **Tailwind source detection is scoped to `src/`** (`source(none)` + `@source "../"`), because tests contain deliberately-wrong example class strings that were generating dead utilities. A future source root outside `src/` will need its own `@source` line — noted in `globals.css`.
5. **Development never fakes credentials.** `UnconfiguredAuthProvider` refuses honestly; an unconfigured deployment returns 503, distinct from a 401 for a bad token; a provider cannot be enabled without the config to run it. There is no dev bypass anywhere, asserted by test.

**Review record.** Round 1 `FAIL` (14 defects, including a functional Tailwind v4 bug that made every primitive render square, and a responsive shell that made approvals unreachable on tablet). Round 2 `FAIL` (3 defects, one of which was the same Tailwind bug reintroduced inside its own fix). Round 3 `PASS`, with every new suite mutation-tested by the reviewer and none found vacuous.

**What NOT to change accidentally.** The two auth ports and their boundary tests. The credential-free verification path. `hardware_attested` as a literal with no assignment path. The self-guarding structure of the contrast and CSS-syntax tests.

**Unresolved.** Blocker B-03 (no GitHub App) remains open and is on the critical path for Phase 3. B-01 was closed during Phase 2 — see log entry 0004. Firebase sign-in is wired but untested against the live project, since no credentials are configured in this environment.

**Next intended task.** `F2-01` — Gemini provider over `google-genai`, with `generate_structured` re-validating model output against the Pydantic schema on our side.

### 0004 — Phase 2 in progress: Gemini provider live-verified

**F2-01 complete and verified against the real API.** A structured call and a
stream both succeeded against `gemini-3.7-flash`, and the structured response
passed our own Pydantic validation, not merely the SDK's. Blocker B-01 is closed.

**Correction worth recording.** I twice told the owner that a key beginning `AQ.`
was not a Gemini key, on the belief that keys start with `AIza`. That was wrong:
current Google AI Studio keys are `AQ.`-prefixed and about 53 characters. The
`AIza` form still exists. **Do not validate or reject a Gemini key by prefix** —
`.env.example` now says so explicitly.

**SDK automatic function calling is disabled outright.** The live run surfaced an
SDK warning about AFC. MCPForge never lets the SDK invoke functions on its own:
every action is orchestrated by our deterministic code and gated by persisted
approvals, so leaving AFC enabled-but-unused would be a latent path for the SDK
to act without passing a gate. `automatic_function_calling=disable=True` is now
set on every request.

**Two backends, both real.** `GEMINI_BACKEND=api_key` uses a key;
`GEMINI_BACKEND=vertex` uses Application Default Credentials against a GCP
project and needs no secret at all. Callers cannot tell which is in use. The
Vertex path is preferred where policy restricts key material and is the
recommended production direction; it requires `aiplatform.googleapis.com`
enabled on the project.

**Environment layout.** The backend reads the repo-root `.env` first, then an
optional `services/api/.env` override, so one file at the root configures it.
The web tier reads `apps/web/.env.local`. Both are gitignored.

**Care note.** `.env` was accidentally overwritten twice with `cp .env.example
.env` while scaffolding, destroying a value the owner had already set. Never
copy over `.env`; edit it line-targeted, or write only when it does not exist.

**Next intended task.** `F2-02` — session and conversation model with the store
port and its in-memory adapter.
