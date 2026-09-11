# MCPForge — Project Status

> This file must always represent reality. Percentage advances only after `[REVIEWER / TESTER]` returns `PASS`.

---

## Overall completion

**90%** — **Phase 8 complete.** All seven Phase 8 tickets are `DONE`, including `F8-02`, whose acceptance was a real Confidential Space run: on 2026-09-11 run `cs-20260910-234427-b3b40d` produced a Google-signed token that the MCPForge API verified to `HARDWARE_ATTESTED`. The 90% recorded earlier while `F8-02` was blocked was provisional; it is now earned. Phase 9 (90% → 100%) has not started.

## Current phase

**Phase 8 — Confidential Execution + Trust Layer (80% → 90%)** — **complete.** `F8-01`, `F8-02`, `F8-02a`, `F8-02b`, `F8-03`, `F8-04` and `F8-05` are `DONE`. Blocker B-04 is closed by a real, API-verified attestation run (Context State Log entry 0019).

Phase 8 took **21 review rounds across six tickets** and produced 30 findings. Not one of them was an improper upgrade to `HARDWARE_ATTESTED`: the trust boundary held in every round of every ticket. What failed, repeatedly, was the *description* of a guarantee and the *checking* of it. Four shapes recurred, and they are the phase's real lesson:

1. **A check matching text near a property rather than at it.** `assert "--require-hashes" in dockerfile` passed because of the comment explaining the flag; deleting the flag from the install command left the suite green.
2. **A test agreeing with the thing it tests.** A quarantine count asserted against a fixture that always produced exactly that number, so a hardcoded constant passed. A rendered number compared against the formatter that produced it, so `return "7"` for every metric passed.
3. **Prose asserting an invariant the code did not have** — in six separate rounds, including a comment citing a test that had never been written.
4. **A self-consistent fiction.** `F8-01` and `F8-02b` both checked `google_service_account`, a claim that does not exist in a real Confidential Space token. Every test passed because the fixtures modelled the same wrong shape. It was found by reading Google's documentation, not by running anything.

**Next phase.** Phase 9 — Hardening, Demo and Launch (90% → 100%).

`F8-01` took **four review rounds**. Rounds 1, 2 and 3 returned `FAIL` with 3, 4 and 4 findings. Every finding was a verification failure escaping `verify_attestation_token` as an unhandled exception, or a comment asserting an invariant the code did not have — never an improper upgrade to `HARDWARE_ATTESTED`, which no round was able to produce. Three separate rounds found a security comment claiming more than the code delivered, which is why the module now ties each asserted invariant to the test that fails if it is violated. Round 4 returned `PASS`.

Phase 7 took **ten review rounds**. Rounds 1–6 returned `FAIL` (10, 4, 3, 3, 2 and 1 findings); the route-enumeration approach that produced rounds 4–6 was removed at the project owner's direction and replaced with a route-independent property test. Rounds 7, 8 and 9 then returned `FAIL` with 6, 2 and 2 findings — every one of them found by mutating the source and observing that the suite stayed green, and three of them being gaps that a previous round's *fix* had introduced. Round 10 returned `PASS`.

## Current ticket

None. Phase 8 is complete. Next is Phase 9 — Hardening, Demo and Launch (90% → 100%). Known carry-forwards: `F9-01` (approval-consuming stages; `F7-02`/`F7-03` are `DONE, PARTIALLY BLOCKED`), `F9-03` (Playwright E2E, still absent), `F6-05` (needs a public URL), and — new with `F8-02` — **running repository jobs inside the attested boundary**: the attested workload performs preflight and attestation only, and `ConfidentialSpaceSecureExecutor` refuses every job in every state.

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
| F2-01 | Gemini provider | PASS (round 3) — verified live |
| F2-02 | Session and conversation model | PASS (round 3) |
| F2-03 | Chat API with streaming | PASS (round 3) — verified live |
| F2-04 | Workspace chat UI and activity timeline | PASS (round 3) |
| F2-05 | Approval interaction UI | PASS (round 3) |
| F3-01 | GitHub App integration | PASS (round 3) — verified live |
| F3-02 | Repository binding and boundary | PASS (round 3) |
| F3-03 | Secret and path filtering | PASS (round 3) |
| F3-04 | Secure execution provider (development) | PASS (round 3) |
| F3-05 | Repository indexer | PASS (round 3) |
| F3-06 | Context retrieval | PASS (round 3) |
| F3-07 | Demo project ingestion | PASS (round 3) |
| F3-08 | Firestore store adapter | PASS (round 3) — verified live |
| F4-01 | Agent framework and base contract | PASS (round 3) |
| F4-02 | Codebase Analyst | PASS (round 3) — verified live against Gemini |
| F4-03 | Workflow Architect | PASS (round 3) |
| F4-04 | Security Reviewer and Human Interaction | PASS (round 3) |
| F4-05 | Orchestrator and state machine | PASS (round 3) |
| F5-01 | WebMCP tool contract model | PASS (round 3) |
| F5-02 | WebMCP Generator | PASS (round 3) — output compiles in the real demo app |
| F5-03 | Patch representation and diff | PASS (round 3) |
| F5-04 | Framework adapter interface | PASS (round 3) |
| F6-01 | Deterministic policy engine | PASS (round 3) |
| F6-02 | Branch and PR writer | PASS (round 3) |
| F6-03 | Access mode elevation flow | PASS (round 3) |
| F6-04 | Rollback and failure handling | PASS (round 3) |
| F7-01 | WebMCP adapter | PASS (round 10) |
| F7-02 | MCPForge read tools | PASS (round 10) — `DONE, PARTIALLY BLOCKED`, see `F9-01` |
| F7-03 | MCPForge gated mutation tools | PASS (round 10) — `DONE, PARTIALLY BLOCKED`, see `F9-01` |
| F7-04 | Agent-origin activity labelling | PASS (round 10) |
| F7-05 | Repository selector UI | PASS (round 10) |
| F8-01 | Attestation evidence model | PASS (round 4) |
| F8-02a | Confidential Space workload image | PASS (round 7) |
| F8-02b | Confidential Space infrastructure and workload identity | PASS (round 2) — script written and plan-verified; never applied |
| F8-03 | Trust panel | PASS (round 2) — renders `DEVELOPMENT_ISOLATION`, the real state |
| F8-04 | Agent 5: Validator and Agent Readiness Score | PASS (round 3) |
| F8-05 | Before/after demonstration | PASS (round 3) |
| F8-02 | ConfidentialSpaceSecureExecutor | PASS (code review round 2) and a real run, `cs-20260910-234427-b3b40d`, verified by the API to `HARDWARE_ATTESTED` on 2026-09-11 |

## In progress

None.

## Pending

Phases 8–9, tickets `F8-01` through `F9-05`, plus `F6-05` (GitHub webhook, needs a public URL). See `05_FEATURE_TICKETS.md`.

**Phase plan:** ten phases (0–9), 10% each, summing to 100%. Phase 9 — Hardening, Demo and Launch — was added during the Phase 0 review after the reviewer found the original plan stopped at 90%.

---

## Blockers

| ID | Blocker | Impact | Status |
|---|---|---|---|
| B-01 | ~~No Gemini API key~~ — **resolved** | Key supplied and verified with a real structured call and a real stream against `gemini-3.7-flash`. Vertex/ADC is also implemented as a no-secret alternative | Closed |
| B-02 | ~~No Firebase project~~ — **resolved** | Firebase project created, Google sign-in enabled, ADC configured locally. The quota project was originally `launchforge-tee`; MCPForge is now pinned to the single canonical project `mcpforge-aa5c2` (see the Google Cloud identifiers section below) | Closed |
| B-05 | Service-account key downloads blocked by organization policy | No impact — the architecture was changed to need none. Token verification uses Google's public JWKS; other server-side Google access uses ADC | Closed by design change, not outstanding |
| B-03 | ~~No GitHub App~~ — **resolved** | App 4797679 registered and installed on `tony19053000`, scoped to selected repositories. Verified live: contents=write, pull_requests=write, metadata=read, and nothing else | Closed |
| B-04 | ~~No verified Confidential Space run~~ — **resolved** | Closed 2026-09-11 by run `cs-20260910-234427-b3b40d`: production `confidential-space` image on `n2d-standard-2`, AMD SEV, `us-central1-b`, booting `workload@sha256:cebf7ea1…`. The workload obtained a Google-signed token from the launcher and delivered it; the MCPForge API verified it — RS256 against Google's published keys, issuer, audience equal to the API-issued nonce, expiry, `swname=CONFIDENTIAL_SPACE`, `dbgstat=disabled-since-boot`, `STABLE` support, `hwmodel=GCP_AMD_SEV`, the exact digest from the API's own configuration, and the workload service account — reaching `HARDWARE_ATTESTED`. A second verification of the same run was refused with `RUN_ALREADY_CONSUMED`. The VM was deleted; no instance or disk remains | Closed |

None of these block Phase 1. Work continues on everything that can be built and tested without them.

---

## Google Cloud identifiers — canonical

MCPForge uses **one** Google Cloud project. Every environment variable, image
path, service account, Artifact Registry path, attestation policy claim, test
fixture and document uses these values and no others.

| Item | Value |
|---|---|
| Project | `mcpforge-aa5c2` |
| Region | `us-central1` |
| Artifact Registry repository | `mcpforge-executor` |
| Image path | `us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor/<image>` |
| Workload service account | `mcpforge-workload@mcpforge-aa5c2.iam.gserviceaccount.com` |
| Fixture zone | `us-central1-a` |
| Launch zone | `us-central1-b` (the zone of the verified run; `launch.sh`) |
| Trusted workload digest | `sha256:cebf7ea1fcb0e898142041507c3a77b7590651a2fb03f14e8f82be548ef89765` |

**`launchforge-tee` and `launchforge-secure-executor` are not MCPForge
resources and must never be referenced.** `launchforge-tee` is a real,
accessible project in the owner's account rather than a dead placeholder, so a
stray reference works silently instead of failing loudly — which is precisely
why it is banned by name rather than left to care. `europe-docker.pkg.dev` and
`europe-west4` are likewise not MCPForge paths.

An audit during Phase 8 found ten references to the wrong project across `.env`,
the attestation and Gemini test fixtures, and this file. All were corrected;
Context State Log entry 0002 keeps its original wording with an annotation,
because the log records what was true at the time.

---

## Tests

| Check | State |
|---|---|
| Unit | **1560 passing** — 262 web (Vitest/RTL), 1298 API (pytest), 4 skipped (2 web, 2 API — the second API skip is the opt-in live check against Google's JWKS). The image tests run under `MCPFORGE_IMAGE_TESTS_REQUIRED=1` in CI, which makes them fail rather than skip when Docker is unavailable. The 2 web skips are the live cross-tier check below, which runs rather than skips in CI. A further 15 run against live Firestore when opted in |
| Integration | Covered within the suites above: FastAPI routes over ASGI transport with real RS256 tokens; SSE chat streaming; store conformance suite |
| Live | Real Gemini structured call and stream, and a full real chat round trip through the API, both via manual scripts in `services/api/scripts/` |
| Live cross-tier | `apps/web/tests/live-e2e.test.ts` — the real WebMCP tools, through the real adapter, over real HTTP against a running FastAPI server (`services/api/scripts/live_api.py`). The web CI job starts that server and sets `MCPFORGE_LIVE_REQUIRED=1`, which makes the check **fail** rather than skip when nothing is listening; locally it skips so a developer who did not start a server is not shown a red bar. Not browser E2E — there is no browser |
| E2E | Not started. Playwright is introduced at `F9-03`; there is deliberately no failing `test:e2e` script in the meantime |
| Build | `npm run build` clean (web only — Python has no build step) |
| Lint | `eslint` clean; `ruff check` and `ruff format --check` clean |
| Typecheck | `tsc --noEmit` clean under strict; `mypy` strict clean |

---

## Security state

| Control | State |
|---|---|
| Secure execution | `DevelopmentSecureExecutor` implemented: path jail, executable allowlist, no parent environment, CPU/memory/wall-clock/output limits, ephemeral workspace. Trust level `DEVELOPMENT_ISOLATION`, honestly reported |
| Client bundle | Contains the Firebase Web config (`NEXT_PUBLIC_FIREBASE_*`) and `NEXT_PUBLIC_AUTH_PROVIDERS` — public browser identifiers by design, required for sign-in. Verified free of the Gemini key, any model SDK, and any service-account or private-key material. Enforced by a CI step that scans the built bundle |
| Auth enforcement | Server-side on every authenticated route; RS256 pinned; `alg:none`, expired, wrong-issuer, wrong-audience and forged tokens all rejected by test |
| Model tool invocation | SDK automatic function calling explicitly disabled, asserted by test |
| Model output as authorization | Gates load an `Approval` from the store by id, check it belongs to this session and project, and check its hash still matches. The interaction agent has no field in which to express an approval. Two independent checks, and the guarantee is their conjunction. (1) Route-independent AST sweep over every backend module: only `api/approvals.py` may assign a decision field, copy one on via `model_copy(update=...)`, or call `create_approval` / `update_approval` / `decide_approval`. It matches on names, so it catches straightforwardly-written paths — a second router, a refactor, a generated route — and is not claimed to defeat deliberate indirection such as `getattr`; no name-based check can. (2) Behavioural gate tests that read the **stored record** after driving every agent endpoint, and so hold however a write was spelled: the approval stays `PENDING`, the gate stays shut, and no agent endpoint transitions the session |
| Risk classification | Re-derived from the mapped function by the policy engine itself, not read from the model's field. The stricter of the two verdicts wins |
| Generated code | Every model-authored string passes through `generation/escaping.py` before becoming part of a file. Generated output is scanned for credentials before it is emitted. Generated tools validate declared types at runtime, not only presence |
| Approval binding | Decisions bind to the artifact hash shown; a changed artifact closes the gate. Actor comes from the verified token, never a request body |
| Chain-of-thought | Never sent by the API and never rendered by the UI. Both tiers assert it independently |
| Attestation | **Real, and verified by a relying party outside the TEE.** `verify_attestation_token` is still the only producer of `HARDWARE_ATTESTED`, pinned by the AST sweeps. The MCPForge API issues each run's nonce, the workload delivers a Google-signed token through a private bucket, and the API verifies it against the nonce it issued and the digest in its own configuration, once. Demonstrated on real hardware on 2026-09-11 (run `cs-20260910-234427-b3b40d`). `HARDWARE_ATTESTED` is per run and lasts only while the verified token is in date; otherwise, and by default, the product reports `DEVELOPMENT_ISOLATION`. The attested workload runs **no** repository job, so no job has yet executed inside the attested boundary. `/healthz` still reports `hardware_attested: false`, because it describes the development executor the service runs by default |
| Secret filtering | Implemented. A fixture repository with thirteen planted credentials yields zero secret bytes downstream, and none in the quarantine records either. Quarantined files are never opened, and matching is case-folded — an earlier version read `.ENV`, `ID_RSA` and `Server.PEM` |
| Network isolation | Real, via an unprivileged user+network namespace. Where the kernel disallows it the executor refuses to run rather than claiming an isolation it lacks |
| Repository access mode | Implemented. `READ_ONLY` by default; elevation requires the project owner and records who and when; a demo project can never be elevated |
| Branch protection | Writer implemented and gated. Writes only to `mcpforge/*`, never the default or a protected name, refuses when the default branch is inside that namespace, and creates refs rather than updating them. No force-push or history-rewrite mechanism exists — asserted structurally |
| Write preconditions | Bound repository, `WRITE_PR` mode, both `PATCH` and `PULL_REQUEST` approvals matching this session and covering the patch by hash, and the policy engine passing on the exact patch. An approval never overrides a policy violation |
| Credentials in repository | None. `.env` ignored; `.env.example` contains names only |
| Service-account keys | None, by design. Not created, not committed, not a supported configuration. Server-side Google access uses ADC |
| Auth posture | Provisional Firebase Auth behind a `TokenVerifier` / `AuthProvider` port pair. Backend imports no Firebase SDK and needs no credentials to verify a token |
| Banned dependencies | None present. Verified absent: LangChain, LlamaIndex, CrewAI, AutoGen, `google-generativeai`. Also absent by design: `firebase-admin` |
| Attestation claims | `/healthz` reports `hardware_attested: false` as a literal with no assignment path |

---

## Latest Git commit

`fd9209b` — `feat(F7): close Phase 7 at 80% after reviewer PASS on round 10`

**Correction.** This line previously read `41dad3c`. That is a real commit object — `docs: close Phase 6 at 70% after reviewer PASS` — but it was superseded when that commit was amended into `d9497c4`, leaving it unreachable from `main` (`git merge-base --is-ancestor 41dad3c main` fails). STATUS recorded the pre-amend hash.

A first attempt at this correction claimed the hash was invented. That was wrong: `git log --oneline --all | grep` does not find an orphaned commit, because `--all` walks refs and an amended-away commit is on none. `git log -1 <hash>` and `git cat-file -e <hash>` do find it. Verify a hash's *reachability*, not its existence. Every other hash below was re-checked with `git merge-base --is-ancestor` on 2026-09-03 and is reachable from `main`.

Phase 7 spans `f45f2e5`, `f1925ae` and `a034fce`, plus the round fixes `6b01d41`, `d82c56f`, `658d6ff`, `24028cf`, `5d92d9f`, `b0a9dd2`, the enumerator replacement `8dd7d5a`, the live cross-tier check `d377346`, and the round-7-to-10 fixes committed at close. All of it is verified as of round 10. The closing commit hash is recorded in the line above once written; it is not back-filled by amending, per the correction recorded in this section.

Phase 6 spans `4f18dcf`, `e06dd54` and `1346a88`. Phase 5 closed at `6d09ed7`, Phase 4 at `f8d1f9f`, Phase 3 at `e1976e1`, Phase 2 at `c7937cd`, Phase 1 at `7754003`, Phase 0 at `49e0162`. Every hash here is reachable from `main`.

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

> **Annotation, added during Phase 8.** The quota project recorded above is history, not current configuration. MCPForge is now pinned to the single canonical project `mcpforge-aa5c2`; `launchforge-tee` is a separate, unrelated project and no MCPForge configuration may reference it. This entry is preserved unedited because the Context State Log records what was true at the time.

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

### 0005 — Phase 2: AI workspace and Gemini

**What was built.** The product now holds a real conversation with a real model,
and the approval gate that everything later depends on.

- `gemini/` — provider port, `GoogleGenAIProvider` over `google-genai`, and a
  fake that re-validates exactly like the real one. Two backends: an API key, or
  Vertex over ADC with no secret at all.
- `models/` — Project, Session, Turn, RunEvent, Approval, and the 26-state
  transition table.
- `store/` — port plus in-memory adapter, with a conformance suite parameterised
  by adapter.
- `api/` — projects, sessions, SSE chat, approvals, gate check, events.
- `apps/web` — typed API client with streaming, the `/workspace` route, the chat
  column, the activity timeline and the approval card.

**274 tests** — 130 web, 144 API. Live-verified: a real structured call, a real
stream, and a full real chat round trip through the API.

**Decisions worth keeping.**

1. **An approval binds to an artifact hash.** Approving a plan does not approve a
   changed plan, and does not open a different gate. Approvals deliberately do
   not expire on a clock — the hash is the invalidation mechanism, and a second
   one would be a second thing to get wrong.
2. **The actor comes from the verified token.** Sending `actor_uid` in a request
   body does nothing, asserted by test.
3. **No chain-of-thought, enforced in both tiers independently.** The API does
   not send it; `isRenderableEvidence` refuses to render reasoning-shaped keys
   even if it arrived. Either tier can regress without the other hiding it.
4. **SDK automatic function calling is off, and now tested.** Every action is
   orchestrated by our code behind persisted gates.
5. **`transitions.py` is data, not enforcement.** The orchestrator that consults
   it is F4-05. The docstring and `02_ARCHITECTURE.md` §6 both say so, because a
   module that describes enforcement it does not perform is how false confidence
   starts.

**Review record — two rounds of FAIL, and both mattered.**

Round 1 found nine defects. Four mutations had survived in the Gemini request
config, meaning a control STATUS.md advertised had no test at all. The chat UI
was dead code that no route mounted, so "tokens stream to the UI" was
unobservable in the running product. Writing the missing client test then
surfaced a real leak: stopping a stream early never released the response body.

Round 2 found something worse. **My cancellation test passed with the entire fix
deleted** — it was watching Python's own async-generator teardown, not the
handler, and I had reported the defect cleared on that basis. Replaced with an
async iterator that is deliberately not a generator, so only the handler's
`finally` can close it. Each half of the fix is now independently killable.

Round 2 also caught a STATUS row that had quietly become false: the client
bundle *does* contain the Firebase Web config once `/workspace` mounts the SDK.
Nothing unsafe — those are public identifiers — but the row claimed the bundle
was free of all credential material, which stopped being true.

**A real hole found while fixing that.** The CI credential scan matched only
`AIza`-prefixed keys. The current Gemini key format is `AQ.`-prefixed, so **a
leaked current-format key would have walked straight through the scan.** Both
the tracked-file scan and a new built-bundle scan now cover it.

**What NOT to change accidentally.** The artifact-hash binding on approvals. The
actor-from-token rule. `ExplicitCloseStream` in the chat tests — it is not a
generator on purpose, and making it one silently disables the cancellation
tests. The `finally` (not `else`) in the chat handler. The `AQ.` pattern in both
credential scans.

**Next intended task.** `F3-01` — GitHub App integration. Needs the owner to
register a GitHub App (blocker B-03).

### 0006 — Phase 3: GitHub and safe repository ingestion

**What was built.** MCPForge can now reach a real repository, keep it inside a
boundary, strip its secrets, run jobs against it in a sandbox, and turn it into
a structure an agent can reason about — without ever handing a repository to a
model wholesale.

- `github/` — App client (installation-scoped, short-lived unpersisted tokens)
  and `boundary.py`, the single assertion every repository operation calls.
- `security/filters.py` + `pipeline.py` — path policy and content scanning, run
  before anything is read or indexed.
- `execution/` — `DevelopmentSecureExecutor`: path jail, executable allowlist,
  minimal environment, real network denial, resource limits, guaranteed teardown.
- `indexing/` — `sources.py` (two ingestion sources, one pipeline), `parser.py`
  (tree-sitter), `indexer.py`, `retrieval.py`.
- `store/firestore.py` — persistence, same conformance suite as in-memory.
- `fixtures/demo-hotel-app` — a real Next.js hotel app, now built by CI.
- `api/repos.py` — binding, elevation and revocation over HTTP.

**509 tests** — 130 web, 379 API, plus 33 against the live Firestore database.
Verified live end to end: GitHub → sandbox → clone → filter → index → destroy.

**Decisions worth keeping.**

1. **Filtering is case-folded.** `.ENV`, `ID_RSA` and `Server.PEM` are the same
   files as their lowercase forms and are credentials either way.
2. **A file with a secret is excluded, not scrubbed.** Redacting a match and
   forwarding the rest is how a secret survives a filter.
3. **Network denial is real, and honest where it is not available.** Where the
   kernel disallows unprivileged namespaces the executor refuses to run rather
   than proceeding without the isolation it advertises.
4. **The wall clock kills the process group.** git, npm and next all fork;
   killing only the direct child left grandchildren alive and blocked the read.
   `_kill_group` compares against our own group first, because a child that
   failed to `setsid()` would otherwise make us SIGKILL ourselves.
5. **One boundary function.** A check spread across call sites is a check that
   will be missed at one.
6. **Two ingestion sources, one pipeline.** The demo project is a rehearsal of
   the real path, not a separate track.

**Review record — three rounds, and each found something real.**

Round 1, twelve defects. Two were holes rather than gaps: secret paths were
matched case-sensitively, so `.ENV`, `ID_RSA`, `Server.PEM`, `Secrets/` and
`.SSH/` were opened, read and indexed; and the sandbox claimed "no network for
analysis commands" in three places while enforcing it nowhere.

Round 2, seven defects — **five of them regressions from my own round-1 fixes**,
and two I had reported done without checking. The worst: adding a CI job for the
fixture swallowed the client-bundle credential scan into a job that never builds
the bundle, so it grepped a directory that did not exist, exited 0, and printed
"no server credential". A green tick enforcing nothing, which is worse than no
check. The case-fold fix also regressed `Cargo.lock`/`Gemfile.lock` into being
read, and making network denial real broke the end-to-end script, which I had
not re-run.

Round 3, PASS.

**Why `PermissiveStore` exists** (`tests/test_repos_api.py`). With a store that
filters by owner, the token subject and the project owner are equal by
construction, so replacing `actor_uid=identity.subject` with
`actor_uid=project.owner_uid` is invisible through the normal path. A store whose
ownership check is deliberately defeated is the only way to make the route prove
it derives the actor from the verified token. Same technique as
`ExplicitCloseStream` in the chat tests.

**What NOT to change accidentally.** The case-folding in `classify_path` and the
lowercase sets it depends on. The `os.getpgrp()` guard in `_kill_group`. The
`allow_network=True` on the clone step only. `PermissiveStore` and
`ExplicitCloseStream` — both are deliberately unusual and deleting them silently
disables the property they protect. The bundle scan living in the `web` job.

**Scope moved, not dropped.** `F6-05` (GitHub webhook — needs a public URL) and
`F7-05` (repository selector UI — belongs with the panels that consume it), each
with full fields and a scope note on the ticket it left.

**Next intended task.** `F4-01` — the agent base contract, then the six runtime
agents and the orchestrator that finally consults the transition table.

### 0007 — Phase 4: six-agent orchestration

**What was built.** The reasoning layer, and the deterministic spine that keeps
it from being trusted with anything.

- `agents/base.py` — one shape for every agent. A subclass supplies an
  instruction, a prompt and an optional verify hook, and never touches the
  provider.
- `agents/analyst.py` — Agent 1. Finds workflows; every claim resolves against
  the index or the agent is made to try again.
- `agents/architect.py` — Agent 2, plus `infer_risk_from_function` and
  `reconcile_risk`.
- `agents/security_reviewer.py` — Agent 4, plus `policy_findings` and
  `evaluate_gate`, the deterministic half.
- `agents/interaction.py` — Agent 6, plus `commit_decision` and `gate_is_open`.
- `orchestration/machine.py` — the enforcement point for §6.
- `models/analysis.py`, `models/toolplan.py`, `models/security.py`.

**618 tests** — 130 web, 488 API. Agent 1 verified live against Gemini on the
real demo app: four workflows, correct risk classes, all evidence resolving.

**Decisions worth keeping.**

1. **A subclass cannot reach the provider.** Enforced by an AST test, not by
   convention: no agent may override `run()`, call `generate_structured`, or
   touch `self._provider`. Otherwise an agent could skip validation entirely.
2. **The structural half is not asked of the model.** Routes, handlers, services
   and the call graph come from the indexer and are given *to* Agent 1. Asking a
   model to restate facts we hold exactly adds a way to be wrong for no gain.
3. **Risk is re-derived, never read.** `policy_findings` computes risk from the
   mapped function itself and does not assume `reconcile_risk` has run.
   `approval_required` is absent from the schema Gemini is given, so there is
   nowhere for a model to claim a tool is safe.
4. **Gates load approvals from the store by id.** Never an object handed in by a
   caller, and the record must belong to this session *and* this project.
5. **`WorkflowArchitect.design()` is the only way to get a plan**, so
   reconciliation cannot be skipped by forgetting a step.

**Review record — three rounds.**

Round 1: eight defects, two of them real holes. Gates took an `Approval` object
and trusted it, so anything able to call `transition()` could fabricate one —
and because the artifact hash is content-derived, an approval from another run
over the same repository carried the same hash. Separately, the "deterministic"
policy check read `tool.risk` and `tool.approval_required`, both model-filled,
while a comment claimed otherwise and `reconcile_risk` had no caller at all.

Round 2: four defects, three of them edits I had reported as made that silently
missed. The fourth was worse: my test named "approval from another session" put
the two sessions in different projects, so the project check passed it and the
session check was never exercised — the round-1 hole was still open behind a
test that looked like it covered it.

Round 3: PASS, with 38 mutations and one survivor, that one fail-safe.

**A note on process.** Twice now I have reported an edit as made without
re-reading the file, and once claimed "mutation-verified every fix" when two
were untested. Both were caught. Verify the file after editing it, and do not
write that something is verified unless the mutation was actually run.

**What NOT to change accidentally.** The AST bypass test in
`test_agent_base.py`. The store lookup and the session/project binding in
`_require_approval` — each half is pinned by its own test. `policy_findings`
re-deriving risk. `ProposedTool` lacking `approval_required`. `LongRejectionAgent`
and `PermissiveStore` — both deliberately unusual, and deleting either silently
disables the property it protects.

**Next intended task.** `F5-01` — the WebMCP tool contract model, then Agent 3,
the generator that writes the actual integration code.

### 0008 — Phase 5: the WebMCP transformation engine

**What was built.** The centre of the product: an approved tool plan becomes
TypeScript that runs in a browser and calls the developer's own functions.

- `models/webmcp.py` — the validated tool contract.
- `models/patch.py` — a patch as data, never a filesystem effect.
- `generation/nextjs.py` — the emitter.
- `generation/escaping.py` — the single boundary where model text becomes code.
- `generation/test_template.py` — a test the developer inherits per tool.
- `generation/adapters/` — Next.js, and an honest refusal for everything else.
- `apps/web/src/components/diff/` — the diff view.

**760 tests** — 152 web, 608 API. The generated integration compiles inside the
real demo app, and the generated tests pass there on first run.

**Decisions worth keeping.**

1. **Agent 3 is not a model call.** The judgement was spent earlier: agent 2
   chose the tools and their mappings, and a human approved. Emitting code from
   a validated contract is mechanical, and a template cannot hallucinate an
   import or reimplement logic. It is also deterministic, so regenerating does
   not invalidate an approval for no reason.
2. **Every model-authored string crosses `generation/escaping.py`.** This is the
   whole of the injection defence, and it is one file so it cannot be
   half-applied.
3. **Handlers take raw input and narrow it.** Runtime guards produce typed
   locals, so the call site needs no cast and `register.ts` has none. An earlier
   version used `as never`, which defeated the only remaining defence.
4. **A gated tool never calls through.** It requests approval and returns the
   pending id, and its generated test fails loudly if that changes.
5. **Generated output is scanned for credentials before it is emitted.**

**Review record — three rounds.**

Round 1, ten defects, one of them the most serious thing found in this project
so far: **model-authored titles and descriptions were interpolated raw into
generated TypeScript.** A description ending `*/` closed the JSDoc block and
everything after it became top-level code that compiled cleanly and ran on
module load in the customer's application. Those strings come from an agent
reading a repository we do not control, so a prompt-injected source file was a
path to arbitrary code in someone else's repo.

Round 1 also found: no generated tests, no diff viewer, no secret scan of
generated output, no capability test, `as never` casting away input validation,
an abort-signal contract pinned only by a substring that prose satisfied, and a
landing page claiming three frameworks when one adapter existed.

Round 2, three defects. My first injection *test* stripped comments before
strings, so a `*/` inside a schema string threw off the matching and swallowed
the very code it was looking for — **it passed on a live injection.** Strings
are stripped first now.

Round 3, PASS. 22 mutations, none surviving. The reviewer applied the patch into
a fixture copy, ran the generated tests, then mutated the generated handlers and
confirmed the generated tests catch both properties they claim to protect.

**Three real bugs were found by compiling the output rather than reading it:** a
handler shadowing the function it imported (infinite recursion), a call built in
the wrong argument shape, and a mis-capitalised type name. None were visible on
inspection. `scripts/generate_check.py` exists because of that.

**A note on process.** Three times this phase I reported an edit as made without
re-reading the file, and each time a string replace had silently missed. I now
edit by line number where a match is fragile, and read the line back.

**What NOT to change accidentally.** `generation/escaping.py` and the rule that
nothing interpolates model text directly. The strings-before-comments order in
`code_outside_comments_and_strings`. `scan_generated` running before the patch is
returned. The absence of `approval_required` from `ProposedTool`.

**Next intended task.** `F6-01` — the deterministic policy engine, then the
branch-and-pull-request writer, which is the first code that can change someone
else's repository.

### 0009 — Phase 6: security, patch and pull-request pipeline

**What was built.** The deterministic checks that decide whether a patch may be
written, and the only code in MCPForge that can change a developer's repository.

- `security/policy.py` — nine rules held as data.
- `github/branches.py` — branch naming and the shapes we will write.
- `github/writer.py` — a list of refusals with a little work at the end.
- `github/pr_description.py` — the pull-request body, built not accepted.
- `orchestration/recovery.py` — what to say and do when a write fails midway.

**866 tests** — 152 web, 714 API. No real repository was touched at any point;
the reviewer confirmed `mcpforge-test` still has one branch and zero pull
requests.

**Decisions worth keeping.**

1. **The writer is a list of refusals.** Bound repository, `WRITE_PR` mode, both
   approvals matching session and project and covering the patch by hash, the
   base commit matching the one reviewed, the branch matching our shape in full,
   and the policy engine passing again at write time. An approval says the human
   agreed; it never says the rules do not apply.
2. **Refs are created, never updated.** `create-ref` cannot move an existing
   branch, so a protected branch is out of reach twice over. The single `DELETE`
   is cleanup, reachable only behind `may_delete_branch`.
3. **Cleanup needs two conditions.** The branch is in our namespace *and* this
   run created it. A developer may have their own `mcpforge/` branch, and
   matching a pattern is not permission to delete.
4. **The pull-request body is built, not accepted.** From the plan and the
   patch, and scanned before any GitHub contact, because it is outbound content
   assembled from model-authored descriptions.
5. **Redundant guards are kept deliberately.** Three mutations survived across
   three rounds because a neighbouring check covers the same case. Each is one
   comparison guarding the operation that changes someone else's repository, and
   the combined property is test-pinned. Removing one to improve a mutation
   score would be the wrong trade.

**Review record — three rounds, 65 mutations.**

Round 1, nine defects. Two were real: the approval did not bind the base commit,
so the approved files could be committed onto a different base than the human
reviewed; and the branch was only prefix-checked, while httpx collapses dot
segments, so `mcpforge/../../../other` passed the check and retargeted the
existence probe at an unrelated endpoint — which 404s, and reads as "the branch
does not exist". F6-04 was also entirely dead code: written, tested against
hand-supplied stages, and called from nowhere.

Round 2, two defects. The failure handler caught only `GitHubError`, so an httpx
timeout after the ref existed escaped raw — no outcome, no explanation, no
cleanup, and a branch left behind. The client has a 60-second timeout, so that
is an ordinary path, not an edge case.

Round 3, PASS.

**A process failure worth recording.** Round 2's other defect was mine: an edit
loop matched two lines instead of one and overwrote the §6 paragraph stating
where transition enforcement lives, while the commit message said the tree was
"updated". That was the third time in this project an edit landed somewhere
unintended and was reported as done. The remedy is not a better replace string;
it is reading the file back afterwards, every time.

**What NOT to change accidentally.** The refusal order in `assert_writable` —
nothing is written before all of it passes, and a test asserts GitHub is not
contacted on refusal. `BRANCH_SHAPE` being matched in full rather than by
prefix. The `except (GitHubError, httpx.HTTPError)`. The two conditions in
`may_delete_branch`. The AST route-enumeration test, which catches both
`elevate_to_write` and a bare `model_copy` on `access_mode`.

**Still open.** `F6-05`, the GitHub webhook, needs a publicly reachable URL and
stays `PENDING`. The writer has no HTTP caller yet; it gets one with `F7-03`'s
`create_pull_request` tool, which is where the ticket plan puts it.

**Next intended task.** `F7-01` — the WebMCP adapter, and then MCPForge's own
tools, which is what makes it agent-accessible.

### 0010 — Phase 7: MCPForge drives itself, and still cannot approve

**What was built.** MCPForge's own WebMCP surface — the phase that makes the
product's central claim testable rather than merely stated.

- `apps/web/src/webmcp/adapter.ts` — the one file that knows the API shape.
- `apps/web/src/webmcp/register.ts`, `use-webmcp.ts`, `tools/index.ts` — twelve
  tools, registered on mount, torn down by `AbortSignal`.
- `services/api/src/mcpforge/api/agent.py` — every route an agent tool can
  reach, and the only place `Origin.AGENT` is established.
- `apps/web/tests/live-e2e.test.ts` — the first executed test in which the two
  tiers actually meet.

**1005 tests** — 222 web, 783 API, 3 skipped.

**Ten review rounds.** Rounds 1–6 `FAIL` (10, 4, 3, 3, 2, 1). Rounds 7–9 `FAIL`
(6, 2, 2). Round 10 `PASS`. The three late rounds are the ones worth reading,
because every finding in them was found the same way: **mutate the source, run
the suite, watch it stay green.** Reading the tests would have found none of them.

1. **Round 7.** A second `APIRouter(prefix="/api/agent")` used
   `model_copy(update={"status": APPROVED})` + `store.update_approval(...)`. The
   whole suite stayed green and the gate opened with no human decision. The
   property test matched only attribute assignment.
2. **Round 8.** With that closed, a second router simply *called the project's
   own* `decide_approval(...)`. No obfuscation at all. Suite green, agent
   self-approved, and the timeline recorded `HUMAN:approval.decided` — breaking
   F7-03 and F7-04 at once. `decide_approval` was banned in one file and
   permitted everywhere else.
3. **Round 9.** The round-8 origin fix excluded a *parameter* shadowing a module
   constant but not a *local reassignment* of it. `ORIGIN = Origin(payload[...])`
   inside a handler labelled an agent action `HUMAN`, suite green.

Three of those were gaps introduced by the previous round's fix. That is the
same shape as round 6 and it is the reason this phase took ten rounds.

**Decisions worth keeping.**

1. **Route enumeration was the wrong shape and is not coming back.** It was
   removed at the project owner's direction. Enumerating routes asks "have I
   listed every door?", which is a question that silently goes stale. The
   replacement asks "can a decision be written at all?" over every backend
   module. Do not reintroduce enumeration to fix a future finding here.
2. **The origin check is an allowlist, not a denylist.** `origin=` must be an
   `Origin.<member>`, an unrebound module constant, or a stored record's
   `.origin`. Round 9's finding was closed by inverting rather than by adding
   another pattern. A denylist here has to guess the attack; the allowlist does
   not.
3. **`_bound_names_in` collects every binding form** — assignment, walrus, tuple
   unpack, `for`, `with as`, `except as`, imports, nested defs. It is
   deliberately unusual. **Do not "simplify" it back to a parameter check**; that
   is precisely the round-9 defect.
4. **Two limits are documented rather than chased.** A name-based AST check
   cannot defeat `getattr(store, "update_approval")`, and cannot distinguish a
   stored record's `.origin` from one parsed out of a request body. Both are
   stated in the test docstrings, in `STATUS.md` and in `02_ARCHITECTURE.md`.
   The guarantee is the *conjunction* of the AST sweep and the behavioural gate
   tests — not either alone. `STATUS.md` no longer says "impossible by
   construction", because that was more than the checks could prove.
5. **The live cross-tier test must be able to go red.** It used to skip silently
   when no server was listening while CI never started one — green forever,
   proving nothing. `MCPFORGE_LIVE_REQUIRED=1` now makes it fail, and the web CI
   job starts `scripts/live_api.py` and tears it down under `if: always()`.

**What NOT to change accidentally.** `_bound_names_in` and the inverted origin
allowlist (decisions 2 and 3). The three-name set in sub-check (c) —
`update_approval`, `create_approval`, `decide_approval` — confined to
`api/approvals.py`. The `assert offenders` self-guards on every sub-check; a
sweep that scans nothing reports green. `MCPFORGE_LIVE_REQUIRED` and the CI step
that starts the server. The honesty paragraphs in the test docstrings — they
record real escapes, not hypotheticals.

**Still open.** `F7-02` and `F7-03` remain `DONE, PARTIALLY BLOCKED`. The gate
holds, but nothing consumes an approved `REPOSITORY_BINDING` or
`WORKFLOW_SELECTION`, and no stage persists an artifact, so "human approves →
execution continues" is unmet and the read tools honestly return
`available: false`. That is `F9-01`, not a Phase 7 defect. Playwright E2E is
`F9-03`. `F6-05` still needs a public URL.

**Next intended task.** `F8-01` — the attestation evidence model. `F8-02` stays
`BLOCKED` on B-04 and is never marked done on a simulation.

---

### 0011 — Phase 8 in progress: what "verified" means, defined before anything claims it

**What was built.** `F8-01`, the attestation evidence model — the T7 control, and
the single most misrepresentable claim in the product. `TrustLevel` and
`AttestationEvidence` moved out of `execution/provider.py` into the new
`execution/attestation.py` and are re-exported, so every existing import site is
unchanged. The module adds `AttestationFailure`, `AttestationPolicy` (with
`AttestationPolicyError`), `AttestationOutcome`, the `AttestationKeyResolver` and
`AttestationVerifier` ports, `JwksAttestationKeyResolver`,
`ConfidentialSpaceAttestationVerifier`, and `verify_attestation_token`.

`verify_attestation_token` checks, in order: a non-empty token; an asymmetric
algorithm from a fixed allowlist, refused *before* the signing key is resolved so
`alg:none` and HS256 confusion never reach key material; the prepared key is a
verification key; the signature; issuer; a **single** audience that is exactly
ours — a token addressed to us and to somebody else is refused, because the
audience is a per-run nonce; expiry and not-before with bounded skew; required
registered claims; workload service account; container image digest matched
exactly against a canonical `sha256:<64 lowercase hex>` with no case folding, no
prefix match and no whitespace stripping; hardware model; software stack; and
debug status.

**Decisions made.**
1. **`AttestationOutcome` is the only return shape**, and `rejected()` is the
   only failure constructor. `__post_init__` forbids a raised trust level
   without evidence, so a caller cannot hand-assemble a verified-looking result.
2. **A permissive policy is a programming error, not a soft path.**
   `AttestationPolicy` raises at construction if it has no audience, a
   non-canonical digest, an empty hardware set or a negative skew.
3. **`JwksAttestationKeyResolver` has no default JWKS URL.** Guessing the
   Confidential Space endpoint inside a security control is worse than requiring
   the argument. `F8-02` supplies it.
4. **Enumeration comes from the code, never from recall.** `nbf` was missed by
   two consecutive rounds because the adversarial-claim list was written from
   memory. The matrix now derives the claim set from `attestation.py`'s own AST
   and **errors on an unrecognised receiver** rather than guessing — which is
   what surfaced `header.get("alg")` as a distinct category instead of silently
   miscounting it.
5. **Every asserted invariant names the test that would fail if it were
   violated**, or is bounded to what is demonstrated. The backstop claim now
   reads "no input the test matrix can construct" — a bound on the enumeration,
   not a proof of unreachability.

**Files introduced.** `services/api/src/mcpforge/execution/attestation.py`,
`services/api/tests/test_attestation.py` (150 tests).

**Review gate outcome.** `PASS` on the fourth reviewer round. Rounds 1, 2 and 3
returned `FAIL` with 3, 4 and 4 findings. Every one was a verification failure
escaping as an unhandled exception — `OverflowError` from a `1e30` expiry,
`jwt.InvalidKeyError` slipping past an `InvalidTokenError` catch, a bare
`TypeError` from a non-RSA key, `int(None)` on `nbf`, an `AttributeError` from
an RSA *private* key — or a comment asserting an invariant the code did not
have. **No round produced an improper upgrade to `HARDWARE_ATTESTED`**; the
trust boundary held throughout, and the reviewer's own injection attempts,
30-mutation batteries and ~40 out-of-matrix token shapes could not break it.
Two defects were found by the coder's own sweep rather than the reviewer: a
multi-audience token being accepted, and a case-folded digest match.

**What NOT to change accidentally.** The single-producer rule — exactly one
function may return `HARDWARE_ATTESTED`, and exactly one may construct
`AttestationEvidence`; both are pinned by AST sweeps with non-empty self-guards.
The `AttestationOutcome.__post_init__` invariant. The no-normalisation rule on
the token side; whitespace-only-counts-as-absent is the only exception, and it
has its own test. The absence of a default JWKS URL. The two guards labelled
`UNREACHABLE TODAY` — their comments state exactly what they do not do.

**Open issues.** `F8-02` remains `BLOCKED` on B-04 and is never marked done on a
simulation; nothing calls the verifier yet, and a test asserts that. Clearing
B-04 additionally requires work no ticket describes — a Confidential Space
workload image and the workload-identity infrastructure — so `F8-02a` and
`F8-02b` must be written into `05_FEATURE_TICKETS.md`, with `01_PRD.md` §8
updated in the same commit, before any of it is built. A ~100,000-deep nested
JSON claim reaches the backstop as `VERIFICATION_ERROR`; it is fail-closed and
documented as outside the enumerated matrix.

**Next intended task.** `F8-03` — the trust panel. It renders server state only,
and for now that state is `DEVELOPMENT_ISOLATION`.

---

### 0012 — Phase 8: the workload image, and knowing when to stop hardening

**What was built.** `F8-02a`, the Confidential Space workload image, in
`infra/confidential-space/`: a `Dockerfile` with the base pinned by digest, a
non-root user and a hash-pinned dependency closure; `entrypoint.py`, which
performs preflight and refuses to start without required configuration;
`build.sh`, which builds reproducibly and prints the digest; `dockerfile_scan.py`,
a single shared Dockerfile parser; and `services/api/tests/test_workload_image.py`
(64 tests).

**The digest is the deliverable, and it is `sha256:9dffebfb…`** — reproduced by
the reviewer from a clean clone on a cold builder, and unchanged under an empty
commit dated 2016, a clone path containing spaces, a dirty tree, files back-dated
to 2001, a hostile `SOURCE_DATE_EPOCH` in the environment, `TZ=Pacific/Kiritimati`,
a Turkish locale and a different umask.

> **Annotation, added during `F8-02b`.** That digest is superseded. The image
> contains `mcpforge/execution/attestation.py`, and correcting the
> `google_service_accounts` claim there changed the image, so the current digest
> is `sha256:31ed4925d78c88080870b5f0846956833a7ad5dd8f9f387997f423c71c5f1eb2`
> and is recorded in entry 0013. `sha256:9dffebfb…` is left in place above
> because it is what `F8-02a` actually produced, reviewed and committed at
> `07b3dab`, and the reproduction described in this paragraph was performed
> against that value and no other. It was briefly overwritten here by a
> repinning sweep during `F8-02b` — an append-only log edited in place, which
> CLAUDE.md §7 forbids and which the reviewer caught.

**Decisions made.**
1. **Three causes were removed from the digest**, each found by a review round
   and none visible to "build it twice and compare", because in every case both
   builds shared the thing that varied: `COPY`-preserved source mtimes; the
   commit clock (`SOURCE_DATE_EPOCH` was `git log -1 --pretty=%ct`, so a single
   empty commit changed the digest — this ticket's own landing commit would have
   invalidated the pin it exists to produce); and a stale BuildKit cache, which
   had already put a wrong digest into the README once.
2. **`allow_cmd_override=false`** is the load-bearing launch-policy label.
   Without it an operator keeps the attested digest and runs a different command
   inside it, so the attestation would verify while proving nothing about
   behaviour.
3. **One shared Dockerfile parser.** Two implementations of one rule drifted in
   opposite directions — Python joined continuations with a space, the shell
   `sed` with nothing; the shell grep was case-sensitive, the Python list was
   not — and a lowercase credential name split mid-token across a continuation
   escaped both. `dockerfile_scan.py` is now executed by `build.sh` and loaded
   by the test suite.
4. **The hardening was stopped deliberately, at the project owner's direction.**
   Rounds 4-6 hardened the Dockerfile *text scanner* against an attacker who, by
   assumption, can edit the tests too. That is defence in depth, not the control.
   The controls that carry the guarantee are the pinned reproducible digest, the
   layer-content scan that reads every layer's real bytes, and `F8-01`'s
   verification — a tampered image yields a different digest and fails
   attestation whatever a Python test noticed.

**Review gate outcome.** `PASS` on the seventh round. Rounds 1-6 returned `FAIL`
with 4, 4, 2, 3, 3 and 4 findings. Two were real and serious: a digest that moved
on unrelated commits, and a credential escape that put an API key inside the
attested image with the whole suite green. The rest were a single recurring
class — **a check matching text near a property rather than at the property** —
which appeared in three consecutive rounds and twice inside its own fixes. The
canonical example: `assert "--require-hashes" in dockerfile` was satisfied by the
comment explaining `--require-hashes`, so deleting the flag from the install
command left the suite green.

**What NOT to change accidentally.** The constant `SOURCE_DATE_EPOCH=0`, set and
never read. `--no-cache` on both builds whose digest can be pinned — removing it
silently re-vacuums the two reproducibility tests. The single shared parser; do
not reintroduce a second keyword list. `allow_cmd_override=false`.

**Stated bound, not a defect.** The Dockerfile text scanner cannot follow a
variable-constructed name. Recorded in `dockerfile_scan.py` and
`03_SECURITY_ACCESS.md` in the same disclosure style as the AST sweeps.

**Open issues.** The image is **not pushed**; the registry is empty and the
recorded digest is local, not registry-confirmed. Confidential Space has never
launched this image, so the launch-policy labels are asserted present and correct
on the artefact, not observed being enforced. The image obtains no attestation
token and runs no repository job.

**GCP state.** Project `mcpforge-aa5c2` is canonical. `iam`, `sts` and
`iamcredentials` were enabled by hand; `F8-02b` must still declare them so a
fresh project reproduces. No workload identity pool, no workload service account,
no Confidential VM, no image in the registry.

**Next intended task.** `F8-02b` — the workload identity pool, OIDC provider and
least-privilege workload service account, under an attribute condition pinning
the image digest, hardware model and debug status. Then `F8-02`, which stays
`BLOCKED` until real Confidential Space execution and attestation are verified
against real infrastructure. It is never marked done on a simulation.

---

### 0013 — Phase 8: workload identity, and a claim name that did not exist

**What was built.** `F8-02b`: `infra/confidential-space/setup.sh`, an idempotent
plan-by-default script that creates the workload identity pool, an OIDC provider
trusting `https://confidentialcomputing.googleapis.com`, the workload service
account, its two project roles, and the pool-to-account binding;
`infra/confidential-space/policy.md`, which records every attribute condition in
prose beside the claim it constrains; `infra/confidential-space/setup_scan.py`, a
shared parser and CEL-subset evaluator used by both the script's self-check and
the tests; and `services/api/tests/test_confidential_space_setup.py` with
`fake_gcloud.py` (55 tests).

**The script has never been applied.** A live plan run against real GCP reports
`6 change(s) would be made. Nothing was changed.` There is no pool, no provider,
no service account, no binding and no VM, and the Artifact Registry repository is
empty. The README's live-verification table records that, and records it as
*not run* rather than as pending.

**The attribute condition**, which is the security core:

```
assertion.swname == 'CONFIDENTIAL_SPACE'
&& assertion.submods.container.image_digest == 'sha256:31ed4925…'
&& assertion.hwmodel in ['GCP_AMD_SEV', 'GCP_AMD_SEV_ES', 'GCP_AMD_SEV_SNP', 'GCP_INTEL_TDX']
&& assertion.dbgstat == 'disabled-since-boot'
&& 'STABLE' in assertion.submods.confidential_space.support_attributes
&& 'mcpforge-workload@mcpforge-aa5c2.iam.gserviceaccount.com' in assertion.google_service_accounts
```

The binding narrows to `attribute.image_digest/<digest>`, not to the whole pool,
so only a workload running that exact image can assume the identity.

**Decisions made.**
1. **The claim is `google_service_accounts` — plural, an array of strings.** The
   first version of this condition, and `F8-01`'s verifier alongside it, used a
   singular `google_service_account`. **No such claim exists in a Confidential
   Space token.** A CEL conjunction over an absent field errors and therefore
   denies, so the condition was not permissive — it was *unsatisfiable*, and the
   federation could never have authorised a genuine token. `F8-01` would have
   rejected every real token for a missing required claim. Every test passed
   throughout, because the token fixtures modelled the same wrong shape: a
   self-consistent fiction, which is the one failure mode a conformance check
   against one's own fixtures cannot detect. It was found by the reviewer reading
   Google's documentation, not by running anything.
2. **`F8-01` was corrected inside this ticket rather than deferred.** Shipping a
   correct condition beside a verifier that rejects every genuine token would
   have left neither half exercisable while the record called the verifier
   `DONE`. `03_SECURITY_ACCESS.md` moved in the same commit.
3. **`roles/logging.logWriter` is not granted.** The draft granted it and
   justified it by saying Confidential Space writes the workload's output to
   Cloud Logging — false while the image sets `log_redirect=never`. Verified
   against Google's documentation: redirection needs both operator metadata and
   that role, and the launch policy overrides the metadata. The absence is
   documented with the trigger that would make it reconsiderable.

**The digest moved, correctly.** The image contains
`mcpforge/execution/attestation.py`, so the claim-name fix changed it:
`sha256:9dffebfb…` → `sha256:31ed4925d78c88080870b5f0846956833a7ad5dd8f9f387997f423c71c5f1eb2`.
`test_the_readme_records_the_digest_that_is_actually_built` caught it, which is
what that test exists for. The reviewer reproduced the new value from a clean
copy on a pruned BuildKit cache, and the 64-test reproducibility matrix passes
against it. Entry 0012 keeps `9dffebfb…` with an annotation, because that is what
`F8-02a` produced and reviewed.

**Review gate outcome.** `PASS` on the second round. Round 1 returned `FAIL` with
the claim-name defect above; round 2 confirmed the fix under a 20-mutation
battery in which reverting the clause to the singular form goes red, so the bug
cannot silently return. Round 2's own finding was that a repinning sweep had
overwritten entry 0012's digest in place — an append-only log edited in place,
which CLAUDE.md §7 forbids. Restored and annotated.

**What NOT to change accidentally.** The plural claim name and the membership
form in both `setup.sh` and `attestation.py`. The narrowing of the principal set
to `attribute.image_digest`. The absence of `roles/logging.logWriter`. The
plan-by-default posture: the script must never mutate without `--apply`.

**Open issues.** B-04 stands. Clearing it now needs three owner actions —
`build.sh --push`, `setup.sh --apply`, and booting a Confidential VM — then
`F8-02`, which is never marked done on a simulation. Nothing has been pushed, no
GCP resource has been created beyond `iam`, `sts` and `iamcredentials` being
enabled, and the product reports `DEVELOPMENT_ISOLATION`.

**Next intended task.** `F8-03` — the trust panel. It renders server state only,
and that state is `DEVELOPMENT_ISOLATION`.

---

### 0014 — Phase 8: the digest moves when the workload moves, and that is the point

**Not a ticket record.** A standing note, because the digest has now changed
twice for the same reason and a future session will hit it a third time.

The workload image contains backend source under `mcpforge/`, so **any change to
a module the image carries changes the image, and therefore the digest.** That is
not churn to be suppressed; it is what a content-addressed digest is *for*. The
pin is what makes attestation mean something, and a pin that survived edits to
the attested code would mean nothing.

Recorded so far:

| Digest | Produced by | Changed because |
|---|---|---|
| `sha256:9dffebfb…` | `F8-02a`, committed `07b3dab` | first reproducible build |
| `sha256:31ed4925…` | `F8-02b`, committed `6c3040c` | the `google_service_accounts` fix touched `execution/attestation.py` |
| `sha256:76a88540…` | `F8-03`/`F8-04` | those tickets touched `execution/provider.py` and `execution/development.py` |

`test_the_readme_records_the_digest_that_is_actually_built` catches every one of
these, which is why each was noticed immediately rather than at a live run.

**When it moves, repin all four live sites** — `infra/confidential-space/README.md`,
`infra/confidential-space/setup.sh`, `infra/confidential-space/policy.md` (three
places: the summary table, the condition block, the principal-set example) — and
regenerate the deliberate near-miss digest in
`services/api/tests/test_confidential_space_setup.py`, which must stay exactly
one character from the real one.

**Do not repin the Context State Log.** Entries 0012 and 0013 record what their
tickets produced and what their reviewers actually reproduced. A repinning sweep
overwrote entry 0012 once and the reviewer caught it: CLAUDE.md §7 makes the log
append-only, and a completed review's record is evidence, not configuration.
Annotate here instead, as this entry does.

**Consequence for B-04, and the reason this matters beyond tidiness.** The digest
pinned in the attribute condition must be the digest of the image actually
pushed. Any backend change after `build.sh --push` invalidates the pin, and the
symptom is not a build error — it is a Confidential VM whose attestation is
refused. So the order is: finish the code, push, then apply the setup, and repin
if anything moves in between.

---

### 0015 — Phase 8: the trust panel says what is true

**What was built.** `F8-03`: `apps/web/src/components/trust/**`, the panel from
`04_FRONTEND_SPEC.md` §8; `services/api/src/mcpforge/api/trust.py`, which reads
real server state; and `apps/web/tests/trust-panel.test.tsx` plus
`apps/web/tests/trust-verified-branch.test.ts`.

**What it renders today**, which is the point of the ticket: *Secure execution —
Development Isolation*, with a neutral `ⓘ unverified` badge and an explicit
**Not hardware-attested** line. No success tone and no green tick anywhere while
unverified. That is not a placeholder awaiting better news; it is the true state,
because nothing in the product has ever obtained an attestation token.

**Decisions made.**
1. **One verified branch, pinned by an AST sweep**, mirroring F8-01's backend
   rule. `trust-verified-branch.test.ts` sweeps the web source for the reserved
   phrase and for any comparison against `HARDWARE_ATTESTED`, with a non-empty
   self-guard. The reviewer added a second component rendering the phrase, then
   moved the phrase into the *unattested* branch of the existing file, and both
   were caught. Same stated bound as the backend sweeps: it matches names, and
   no name-based check defeats deliberate indirection.
2. **Every row is server state, verified by mutation.** Hardcoding the
   quarantine count, forcing `active`, or flipping the trust level each turns a
   test red. The green tick on the WebMCP row is for browser feature detection,
   which is genuinely observed, and the mock branch precedes it.
3. **Paths only, never contents.** `quarantined_paths_of` copies one field and
   only string entries. Two tests assert the planted secret and a
   `BEGIN PRIVATE KEY` marker are absent from the raw response body.

**Review gate outcome.** `PASS` on the second round. Round 1 returned `FAIL` with
one finding, and it is worth recording because it is a third distinct variant of
this phase's recurring defect: **a test that passes because a fixture happens to
match a constant.** `test_the_quarantine_count_is_the_filter_pipelines_own_record`
asserted `quarantined_count == len(result.quarantined_paths)` against a fixture
that always quarantines exactly three files, so replacing the endpoint's
`len(paths)` with a literal `3` left the entire API suite green. The acceptance
criterion says "the quarantine count is real", and on the backend it was not
pinned at all — the web tier had pinned its half correctly. Fixed by
parametrising over two fixtures with different counts, so no constant satisfies
both, plus a guard that fails loudly if the pipeline ever stops varying the
count. The reviewer confirmed by disabling `.pem` detection at both routes.

**What NOT to change accidentally.** The single-branch rule and its sweep's
self-guard. The `is not None` guards around trust state — a truthy coercion was
one of the mutations that had to fail. The parametrisation of the quarantine
count: a single fixture cannot distinguish a real count from a lucky constant.

**Open issues.** None for this ticket. The panel will render the verified state
only when `F8-02` produces a real `HARDWARE_ATTESTED`, which needs B-04 cleared.

**Next intended task.** `F8-04` is `IN_REVIEW`; `F8-05` follows it.

---

### 0016 — Phase 8: a score computed from what actually ran

**What was built.** `F8-04`, agent 5:
`services/api/src/mcpforge/agents/validator.py` runs the check suite inside the
secure workspace with no outbound network — registration, discovery, schema
validity, execution, invalid-input rejection, authorization gates, UI
synchronisation, the regression suite, build and typecheck — and
`services/api/src/mcpforge/orchestration/scoring.py` turns executed results into
the Agent Readiness Score. `services/api/tests/test_validator.py` is 60 tests.

**The four properties, each pinned by a named test that was made to fail.**
1. **The verdict is `CommandResult.exit_code`.** The suite contains deliberately
   persuasive output — `PASS: 7/7 checks green. Agent Readiness 100/100.` beside
   a non-zero exit, and an apologetic failure text beside a zero — and the score
   follows the exit code both times.
2. **A component with no evidence scores zero, labelled.** Not a default, and a
   skipped check is a separate record carrying its reason.
3. **Gemini is never asked for a score.** An AST sweep asserts no backend module
   both prompts Gemini and touches the score, with non-empty self-guards. The
   reviewer planted a `GeminiProvider`-based scorer and it was named and caught.
4. **The weights are §11's**, parsed out of `02_ARCHITECTURE.md` rather than
   compared against the code's own constant, and they sum to 100. A swap that
   preserved the sum still failed.

**The defect that mattered, and why it is the phase's recurring shape.** A tool
with **no inputs** scored the full 10 ERROR_HANDLING points for a check that was
never generated. `rejection_test_name` returned `""` for such a tool; the caller
guarded with `is not None`, so `""` passed, and the command became `-t ''` —
which vitest treats as matching *everything*, so the execution test's green
result was scored as error handling. It now returns `None`, and the absent check
is recorded as a skip with a reason so the report shows an absence rather than a
component quietly not appearing.

**A measured fact worth keeping.** `passWithNoTests: false` does **not** cover a
`-t` name filter that matches nothing. vitest finds the file, marks every test in
it skipped, and exits **0** — `Tests  2 skipped (2)`. So a renamed or deleted
test would have scored full marks for a check that ran nothing.
`vitest_ran_a_test` catches that and records such a run as skipped. Getting the
detection right took two wrong attempts: a regex whose `\s+` gave back a space so
a negative lookahead missed, and then a pattern that counted `skipped` as having
run. Real output settled it, not reasoning about it.

**Decisions made.**
- **The generated test names live in one place.** `AUTHORIZATION_TEST_NAME`,
  `EXECUTION_TEST_NAME` and `rejection_test_name` are defined in
  `generation/test_template.py` and read by both the generator and the
  validator's selectors, because two copies of that rule is how one stops
  matching the other.
- **`resolve_inside` is the one path-jail implementation**, in
  `execution/provider.py`, delegated to by the executor and by anything that
  writes into a workspace.
- **Evidence coercion stays, and the rule is enforced over source.** Making
  `ExecutedCheck.evidence` reject a mapping broke legitimate round-tripping, and
  a validation context would put the discriminator in the hands of whoever wants
  to bypass it. Instead two AST sweeps — one for a second `CheckEvidence(...)`
  call site, one for a dict literal in the `evidence` position — with the bound
  stated: bare and attribute-qualified calls are matched; an aliased import of
  the class, a variable, or `getattr` are not.

**Review gate outcome.** `PASS` on the third round. Rounds 1 and 2 returned
`FAIL` with 5 and 1 findings. Four of those six were prose asserting more than
the code did, including a comment citing a test that had never been written and
two restatements of the `passWithNoTests` belief that had already been
disproved. The last was a matcher narrower than the bound printed beside it —
it matched `ExecutedCheck(...)` but not `scoring.ExecutedCheck(...)`, which is
not indirection, just the other ordinary spelling.

**What NOT to change accidentally.** `rejection_test_name` returning `None`
rather than `""`. The `vitest_ran_a_test` pattern excluding `skipped`. The shared
test-name constants. Both AST sweeps and their self-guards. The weights test
parsing `02_ARCHITECTURE.md` rather than trusting the constant.

**Next intended task.** `F8-05` — the before/after demonstration, the last
buildable ticket in Phase 8. `F8-02` stays `BLOCKED` on B-04.

---

### 0017 — Phase 8 closes: the before/after demonstration, and what the phase cost

**What was built.** `F8-05`:
`services/api/src/mcpforge/orchestration/benchmark.py` measures agent
interaction with the application before and after transformation — interaction
steps, tasks attempted and completed, errors, retries, approval points, elapsed
time — and `apps/web/src/components/report/**` renders the comparison.
`services/api/tests/test_benchmark.py` is 38 tests;
`apps/web/tests/benchmark-comparison.test.tsx` and `report-format.test.ts` are
12 more.

**The design decision that carries the ticket.** `MetricCell` is a discriminated
union whose not-measured member has **no `value` field at all**. "Absent rather
than defaulted" therefore holds by construction: there is no field for a zero to
hide in, and `test_an_absence_has_no_value_field_to_default` asserts it across
`model_fields`, `hasattr` and `model_dump`. `format.ts` refuses to render a
sub-10ms measurement as `0.00`, because a real measurement displayed as zero is
the substitution this ticket exists to prevent.

**Both runs are sandboxed identically**, refused twice — before execution in
`compare` and again at construction in `BenchmarkReport` — and neither may have
outbound network. `BenchmarkReport._check` rejects a measurement whose `run_id`
names no run on that side, so traceability is enforced rather than assumed.

**Review gate outcome.** `PASS` on the third round; rounds 1 and 2 returned
`FAIL` with 1 and 3 findings. **Every one was a test that could not fail, and
all four were in the rendering tier while the measurement tier was sound
throughout.**
- A test that called `render()` and then never queried the DOM — every assertion
  was against its own JSON fixture. Making the component render `±0` across
  every unmeasured row left all four tests green: a change computed against a
  side nobody measured, the exact figure the ticket forbids.
- An assertion of the form `element.textContent === formatValue(record.value,
  record.unit)`, which puts the formatter on both sides of the equality.
  Replacing its body with `return "7";` — every number on screen a 7 — passed.
- The Change column never compared to `row.delta`. Forcing the sign to `+`
  rendered a six-error *reduction* as an increase, inverting the direction of
  the headline comparison, with everything green.
- The sub-10ms rule asserted in prose with nothing exercising the branch.

The remedy in each case was the same: pin the formatter independently against
**literal strings**, and derive expected values in the test rather than from the
code under test.

**What NOT to change accidentally.** `Absence` having no `value` field. The
sub-10ms branch in `formatSeconds`. The independence of `report-format.test.ts`
— it is what makes the comparison in `benchmark-comparison.test.tsx` sound. The
`data-cell` / `data-metric` / `data-run-id` / `data-recorded-at` attributes;
they are how a test reaches a record rather than other rendered text.

**Phase 8 is complete except `F8-02`.** 21 review rounds across six tickets, 30
findings, and **not one improper upgrade to `HARDWARE_ATTESTED`** — the trust
boundary held in every round of every ticket. What kept failing was the
description of a guarantee and the checking of it.

**Clearing B-04 now needs three owner actions**, in this order, because the
digest pinned in the attribute condition must match the image actually pushed:
`bash infra/confidential-space/build.sh --push`, then
`bash infra/confidential-space/setup.sh --apply`, then booting a Confidential VM
(N2D or C3D with AMD SEV). Only then can `F8-02` fetch a real attestation token
and verify it. It is never marked done on a simulation.

**Next intended phase.** Phase 9 — Hardening, Demo and Launch (90% → 100%).
`F9-01` (the approval-consuming stages that leave `F7-02` and `F7-03`
`PARTIALLY BLOCKED`), `F9-03` (Playwright E2E, still absent) and `F6-05` (needs
a public URL) are the known carry-forwards.

---

### 0018 — F8-02, code side: a token the service verifies, not one the image vouches for

**Not a completion.** `F8-02` stays `BLOCKED`. This entry records that its code
side passed review; the ticket completes only when a real Confidential Space run
produces a token the API service verifies.

**Why this work happened.** The owner launched a paid Confidential Space VM on
2026-09-10 against the pushed image `sha256:76a88540…`. It failed and was
deleted. Two blockers: (1) the workload refused to start for missing runtime
configuration — the launch command it was given carried no `tee-env-*` values;
(2) no code requested an attestation token at all.

**What was built.**
- **Token retrieval inside the workload** through the official launcher
  interface: `POST http://localhost/v1/token` over
  `/run/container_launcher/teeserver.sock`, body `{"audience", "token_type":
  "OIDC"}`, raw JWT back. Standard library only, a 30 s watchdog over the whole
  exchange, a 64 KiB body cap, and the response must be exactly one compact JWS.
  Every failure is named and yields no token.
- **The relying party is the API service**, `services/api/src/mcpforge/relying_party/`.
  `python -m mcpforge.relying_party begin` issues a run id and a random 128-bit
  audience — the nonce — and records the run pending. `launch.sh --run-id <id>`
  obtains the audience only from that record and refuses an unknown, verified or
  expired run; there is no audience argument.
- **Transport is a private bucket.** The workload writes the raw token,
  create-only, to `gs://mcpforge-aa5c2-attestation/attestation/<run_id>.jwt`. The
  token is Google-signed, so the bucket is transport and need not be trusted. The
  workload service account gets `roles/storage.objectCreator` on that one bucket
  only; it cannot read, delete or overwrite.
- **Verification** in `POST /api/attestation-runs/{id}/verify` and `relying_party
  verify`, through `verify_attestation_token`, against the audience the API
  issued, the digest in **the API's own configuration**
  (`CONFIDENTIAL_SPACE_IMAGE_DIGEST`), the workload service account, Google's JWKS
  from the discovery document, and expiry. **Replay is refused twice**: a
  consumed-record check and an atomic pending-to-consumed rename.

**Decisions made.**
1. **In-TEE self-verification was rejected.** The first implementation verified
   the token inside the workload and never let it out. The reviewer returned FAIL:
   an image that checks its own token proves nothing to anyone outside, because a
   malicious image would simply report success. The owner decided the
   relying-party channel belonged in this work so one paid run proves the whole
   path.
2. **`MCPFORGE_EXPECTED_IMAGE_DIGEST` was added and then removed.** Once the
   relying party pins the digest from its own configuration, an operator-supplied
   digest adds nothing a relying party can use. `allow_env_override` is back to
   the two per-run values.
3. **The path-jail root is an in-code constant, `/workspace`**, not an image
   `ENV`. It cannot be set by an operator, and the entrypoint refuses if
   `MCPFORGE_WORKSPACE_ROOT` is set at all. The failed run reportedly listed it as
   missing; the pushed image did carry it, and Google's launcher source passes
   image `ENV` through, so that report remains **unexplained** — the constant
   removes the dependency rather than explaining it.
4. **`launch.sh` launches the production `confidential-space` family.** It was
   written against the debug family, which reports `dbgstat=enabled`; the relying
   party requires `disabled-since-boot`, so a debug run could never clear B-04.
   The review caught this before any money was spent on it. A production VM still
   writes the launcher's exit status to the serial console, and it stops when the
   workload ends.
5. **No metadata-server attestation path.** The metadata server supplies only an
   OAuth access token for the bucket upload, confined to
   `execution/token_delivery.py` and visibly separate from token retrieval.

**Runtime variables for the next VM**: `tee-env-MCPFORGE_RUN_ID` and
`tee-env-MCPFORGE_ATTESTATION_AUDIENCE`, both issued by the relying party and
written into the command by `launch.sh`. Nothing else.

**Digest.** `sha256:76a88540…` is in the registry and pinned by the live provider
from the earlier `setup.sh --apply`. The image this work produces is
`sha256:cebf7ea1fcb0e898142041507c3a77b7590651a2fb03f14e8f82be548ef89765`,
reproduced by the reviewer from a clean clone on a fresh builder, and pinned in
every live site. It is **built locally, not pushed.**

**Review gate outcome.** `PASS` on the second code review. Round 1 found the
design sound for safety but not for purpose (self-verification), plus three
documentation defects; round 2 confirmed genuine relying-party verification,
least privilege, replay refusal, no weakening of `attestation.py`, both F8-01
sweeps intact, and no GCP change. The debug-family finding was applied after the
verdict as a one-line change outside the image, with the launch test proven to
fail if it reverts.

**What still needs a real VM to confirm.** The launcher accepting a digest-pinned
`tee-image-reference` (read from launcher source, not observed); `tee-env-*`
values reaching the workload; the exact bytes the launcher returns; the workload
reaching `storage.googleapis.com` and Google's JWKS; and the claim names in a
real token, against the policy.

**Owner actions, in order.** `bash infra/confidential-space/build.sh --push`,
then `bash infra/confidential-space/setup.sh --apply`, then issue a run with
`python -m mcpforge.relying_party begin` and launch it with `launch.sh --run-id
<id> --apply`. Delete the VM afterwards.

**What NOT to change accidentally.** The relying party as the only route to
`HARDWARE_ATTESTED`; the API-issued nonce with no audience argument on
`launch.sh`; the digest taken from the API's configuration, never the token; the
two replay guards; the bucket-scoped create-only grant; the production image
family.

---

### 0019 — F8-02 closes on real hardware, and B-04 is resolved

**What happened.** One paid Confidential Space run, authorised by the project
owner, on 2026-09-11. Run `cs-20260910-234427-b3b40d` was issued by the MCPForge
relying party, launched by `launch.sh` as
`mcpforge-cs-20260910-234427-b3b40d` — production image family
`confidential-space`, `n2d-standard-2`, AMD SEV, `us-central1-b` — booting
`us-central1-docker.pkg.dev/mcpforge-aa5c2/mcpforge-executor/workload@sha256:cebf7ea1…`.

**The evidence.**
- The launcher's own log shows the image pulled by exactly that digest, the
  launch policy parsed and applied — as far as the launcher reports; no
  forbidden override was attempted, so a refusal was not observed (only `MCPFORGE_RUN_ID` and
  `MCPFORGE_ATTESTATION_AUDIENCE` overridable, both redacted in its log,
  `log_redirect` never), `/v1/token called`, and "workload task ended and
  returned 0" after 1.9 s. The production image then shut the VM down.
- `python -m mcpforge.relying_party verify cs-20260910-234427-b3b40d` returned
  `verified: true`, `trust_level: HARDWARE_ATTESTED`, `consumed: true`.
- The delivered token's claims, read back without printing the token: RS256;
  issuer `https://confidentialcomputing.googleapis.com`; audience exactly the
  nonce the API issued for this run; one-hour validity; `swname`
  `CONFIDENTIAL_SPACE`; `dbgstat` `disabled-since-boot`; `support_attributes`
  `LATEST`, `STABLE`, `USABLE`; `hwmodel` `GCP_AMD_SEV`; image digest exactly
  `sha256:cebf7ea1…`; `google_service_accounts` exactly the workload service
  account.
- **Replay was refused on the real token.** A second verification of the same
  run returned `RUN_ALREADY_CONSUMED` and `DEVELOPMENT_ISOLATION`.
- The VM was deleted immediately after. No instance and no disk remain, so no
  compute is billing.

**Housekeeping before the run.** `.env` was missing three settings the relying
party needs — `CONFIDENTIAL_SPACE_IMAGE_DIGEST`,
`CONFIDENTIAL_SPACE_WORKLOAD_SERVICE_ACCOUNT` and
`CONFIDENTIAL_SPACE_ATTESTATION_BUCKET` — and would have refused every token.
They were filled from `setup.sh`'s own constants rather than retyped. The
workload service account's `workloadIdentityUser` binding for the retired digest
`sha256:76a88540…` was removed at the owner's direction, leaving only
`cebf7ea1…`, and `setup.sh --verify` reported no drift. The launch zone moved to
`us-central1-b` (commit `a4c2774`).

**What this does and does not establish.** It establishes that attestation is
real end to end: a token Google signed, for this exact image on genuine AMD SEV
hardware in a production (non-debug) Confidential Space, verified by a relying
party outside the TEE against a nonce it chose and a digest it pins. It does
**not** establish that repository jobs run inside the attested boundary — the
workload performs preflight and attestation only, and
`ConfidentialSpaceSecureExecutor` refuses every job in every state. That is
recorded as a Phase 9 carry-forward rather than implied by `F8-02` being done.
`HARDWARE_ATTESTED` is per run and lasts only while the verified token is in
date; by default the product reports `DEVELOPMENT_ISOLATION`.

**One piece of prose deliberately left stale.** The module docstring in
`services/api/src/mcpforge/execution/confidential_space.py` still says the
ticket is `BLOCKED`. That file is inside the attested image, so editing even a
docstring would move the digest and un-trust the image that was just verified.
It will be corrected the next time the image is rebuilt and re-attested for a
real reason. The same applies to every file the Dockerfile copies.

**Unexplained, and recorded rather than guessed.** The first, failed VM run
reportedly listed `MCPFORGE_WORKSPACE_ROOT` as missing although the pushed image
carried it. The path-jail root is now an in-code constant, so the question no
longer affects anything, but it was never explained.

**Completion.** 90%, now earned. Phase 8 is complete; Phase 9 (90% → 100%) has
not started.

**What NOT to change accidentally.** Any file the workload image carries — it
would move the trusted digest. The relying party as the only route to
`HARDWARE_ATTESTED`. The API-issued nonce and the single-use run record. The
production image family in `launch.sh`.
