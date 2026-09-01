# MCPForge — Technical Architecture

**Status:** Phase 0 baseline. Living document — any implementation that changes this must update it in the same commit.

---

## 1. Shape of the system

MCPForge is a **polyglot two-tier application**:

```
Browser (agent-accessible)          Server (never reachable from the browser)
┌──────────────────────────┐        ┌────────────────────────────────────────┐
│ apps/web                 │        │ services/api                           │
│ Next.js · React · TS     │  HTTP  │ FastAPI · Python 3.12 · Pydantic v2    │
│ Tailwind                 │ <────> │                                        │
│ WebMCP tool surface      │  SSE   │ Gemini provider (google-genai)         │
│ (document.modelContext)  │        │ 6 runtime agents                       │
│ Firebase Auth client     │        │ Deterministic orchestrator + FSM       │
│                          │        │ Repository indexer                     │
│                          │        │ Secret/path filter                     │
│                          │        │ GitHub App client                      │
│                          │        │ SecureExecutionProvider                │
└──────────────────────────┘        └────────────────────────────────────────┘
                                             │
                                    Firestore · GitHub API · Gemini API
```

### 1.1 Why Python on the backend

The project owner's stated preference is a Python backend, and it is the right call for this system: the backend's job is agent orchestration, structured-output validation, static analysis and job control, where Pydantic v2 gives us schema-validated LLM output with the least ceremony, and the `google-genai` Python SDK is first-class.

The frontend must be TypeScript/React/Next.js regardless of backend language, because:
- WebMCP is a **browser** API. MCPForge's own WebMCP surface can only live in the browser.
- The MVP's generation target is Next.js/React/TypeScript, so MCPForge dogfoods the exact stack it generates for.

The split is therefore not a compromise; it is the correct boundary. The line is drawn so that **no AI call, credential, repository content, or authorization decision ever exists in the browser tier.**

### 1.2 Cost of the split, and how it is contained

Two languages means two type systems describing the same objects. Contained by:
- Pydantic v2 models in `services/api` are the single source of truth.
- The API emits an OpenAPI schema; `apps/web` generates its TypeScript types from it (`npm run gen:api-types`). Hand-written duplicate interfaces for API payloads are a review failure.

## 2. Repository layout

```
MCPForge/
├── apps/
│   └── web/                    Next.js App Router application (TypeScript)
│       ├── src/app/            routes, layouts, server components
│       ├── src/components/     UI components
│       ├── src/lib/            client utilities, API client, auth
│       ├── src/webmcp/         MCPForge's OWN WebMCP surface (Phase 7)
│       │   ├── adapter.ts      feature detection + real/mock adapter
│       │   ├── register.ts     registration lifecycle
│       │   └── tools/          one file per tool
│       └── tests/              Vitest + RTL; e2e/ for Playwright
├── services/
│   └── api/                    FastAPI backend (Python 3.12)
│       ├── src/mcpforge/
│       │   ├── main.py         app factory, routers, middleware
│       │   ├── config.py       environment validation (fail fast)
│       │   ├── api/            HTTP routers (thin; no business logic)
│       │   ├── agents/         the six runtime agents
│       │   ├── orchestration/  state machine, run loop, events
│       │   ├── gemini/         provider abstraction over google-genai
│       │   ├── indexing/       repository index pipeline
│       │   ├── security/       secret filter, path policy, redaction
│       │   ├── generation/     patch generation + framework adapters
│       │   ├── github/         GitHub App client, branch + PR writer
│       │   ├── execution/      SecureExecutionProvider implementations
│       │   ├── store/          persistence ports + adapters
│       │   └── models/         Pydantic schemas (source of truth)
│       └── tests/
├── fixtures/
│   └── demo-hotel-app/         real Next.js app used as the demo project and test fixture
├── docs/                       supplementary design notes
├── README.md  .gitignore  .env.example
├── 01_PRD.md  02_ARCHITECTURE.md  03_SECURITY_ACCESS.md
├── 04_FRONTEND_SPEC.md  05_FEATURE_TICKETS.md  STATUS.md  CLAUDE.md
├── .claude/agents/             Claude Code development subagents
└── package.json                npm workspace root + cross-stack scripts
```

**Workspace strategy:** npm workspaces for the JS side, `uv` for Python. No Turborepo, no Nx — there is one JS package; a build orchestrator would be pure overhead. Root `package.json` scripts fan out to both stacks for `npm run typecheck | lint | test`. `npm run build` is web-only — Python has no build step, and the backend's equivalent gate is `typecheck` + `test`.

## 3. Stack decisions

| Concern | Choice | Rationale |
|---|---|---|
| Frontend | Next.js (latest stable) + React, App Router | WebMCP is a browser API; App Router is the current default |
| Styling | Tailwind CSS | No component-library dependency in V1 |
| Backend | FastAPI + Pydantic v2, Python 3.12 | Owner preference; best fit for structured LLM output |
| Python tooling | `uv`, `ruff`, `mypy`, `pytest` | Fast, minimal, standard |
| LLM SDK | `google-genai` (Python) | The official current SDK. `google-generativeai` is obsolete and banned |
| Model | `GEMINI_MODEL` env var, default `gemini-3.7-flash` | Verified against ai.google.dev model docs at Phase 0. Never hardcoded in logic |
| Agent framework | None — ours | LangChain/CrewAI/AutoGen are banned in V1 |
| Auth | Firebase Authentication — **provisional**, behind a port | Fastest correct path to a real Google sign-in today. Not committed to for production; see §3.2 |
| Server-side Google credentials | Application Default Credentials (ADC) | Organization policy blocks service-account key downloads. No key file exists or is depended on |
| Store | Firestore behind a port interface; in-memory adapter for tests | Google ecosystem; swappable |
| GitHub | GitHub App, per-repository installation | Scoped access, short-lived installation tokens |
| Web tests | Vitest, React Testing Library, Playwright | |
| API tests | pytest, pytest-asyncio, httpx ASGI transport | |

### 3.1 Gemini provider

`services/api/src/mcpforge/gemini/` exposes a `GeminiProvider` protocol with one real implementation over `google-genai` and a recorded/fake implementation for tests.

```python
class GeminiProvider(Protocol):
    async def generate_structured(
        self, *, system_instruction: str, contents: list[Content],
        schema: type[BaseModelT], trace: TraceContext,
    ) -> BaseModelT: ...
    async def stream_text(self, ...) -> AsyncIterator[str]: ...
```

Rules:
- Structured output uses `response_mime_type="application/json"` with a Pydantic `response_schema`, and the result is re-validated by Pydantic on our side. **A model response is never trusted because the SDK said it matched.**
- The model id comes from config, never a literal.
- Every call carries a `TraceContext` (project, run, agent, step) for the activity timeline.
- Prompt assembly is the only place repository content enters, and it takes **already-filtered** context objects — see §5.

### 3.2 Authentication — provisional by design

Firebase Authentication with Google sign-in is **the current implementation, not the decided architecture.** The likely production direction is direct Google OAuth ("Sign in with Google"), particularly for a Vercel deployment. Phase 1 therefore optimises for *removability* rather than for Firebase depth.

Two ports keep the decision reversible:

```python
class TokenVerifier(Protocol):
    async def verify(self, raw_token: str) -> VerifiedIdentity: ...
```
```ts
interface AuthProvider {
  signIn(provider: ProviderId): Promise<Session>
  signOut(): Promise<void>
  currentSession(): Session | null
  onChange(cb: (s: Session | null) => void): Unsubscribe
}
```

`VerifiedIdentity` is ours — `{ subject, email, email_verified, issuer, claims }` — and is the only identity type the rest of the backend sees. No Firebase type crosses either port. Swapping to Google OAuth means writing one new `TokenVerifier` and one new `AuthProvider`; nothing in the orchestrator, store, or API layer changes.

**Backend verification uses no Firebase SDK and no credentials.** A Firebase ID token is a standard RS256 JWT signed by Google, with a public JWKS endpoint. `FirebaseIdTokenVerifier` validates signature, `iss` (`https://securetoken.google.com/<project>`), `aud` (`<project>`), `exp`, and a non-empty `sub`, using PyJWT with a cached JWKS client. This means:

- No `firebase-admin` dependency for authentication, and therefore no service-account key — which matters, because organization policy blocks downloading one.
- A direct-Google-OAuth verifier is the same code with a different issuer, audience and JWKS URL.
- The client SDK stays confined to `apps/web`; the backend never imports Firebase at all.

**Server-side Google credentials use ADC.** Where the backend needs Google credentials for something other than token verification — Firestore — it uses Application Default Credentials, obtained in development with `gcloud auth application-default login` and in production from the deployment's workload identity. `GOOGLE_APPLICATION_CREDENTIALS` pointing at a downloaded key file is **not** a supported configuration; no such file exists, is committed, or is depended upon.

## 4. Runtime agents

Six roles, one provider. Each agent = system instruction + input schema + strict output schema + deterministic pre/post processing. Agents do not talk to each other; the orchestrator passes validated state between them.

| # | Agent | Input | Output |
|---|---|---|---|
| 1 | Codebase Analyst | filtered repository index + selected snippets | `CodebaseAnalysis` (framework, routes, components, API handlers, services, business ops, forms, call graph edges, candidate workflows, evidence refs) |
| 2 | Workflow Architect | analysis + developer-selected workflows | `ToolPlan` (tools: name, description, JSON Schema input, output contract, risk class, approval requirement, mapping to existing function) |
| 3 | WebMCP Generator | approved tool plan + relevant source | `GeneratedPatch` (file changes, per-file rationale, generated tests) |
| 4 | Security Reviewer | generated patch + tool plan + policy | `SecurityReport` (PASS/FAIL, findings with severity, recommended fixes) |
| 5 | Validator / Test Agent | patch applied in secure workspace | `ValidationReport` (per-check results + evidence) |
| 6 | Human Approval / Interaction | conversation + current run state | `InteractionTurn` (message, offered choices, requested approval id) |

Agent 6 may *interpret* natural language into a proposed decision. It may never *be* the decision — see §6.

Agent boundaries:
- Agents 1, 2, 4, 6 never touch the filesystem.
- Agent 3 emits a patch representation; it does not write to any repository.
- Agent 5's verdict is computed from real command exit codes, not from model text.

## 5. Repository understanding pipeline

**Non-negotiable:** `Repository → deterministic index → relevant context → Gemini`. Never `repository → dump into LLM`.

Two ingestion sources exist, behind a `RepositorySource` port. They converge immediately, before any filtering — everything downstream of the path policy is identical for both, so there is exactly one analysis pipeline:

```
   GitHub source                      Demo source
   scoped clone, shallow,             bundled fixture app copied
   single branch                      into the workspace
          └────────────┬───────────────────┘
                       │  RepositorySource port
   ↓───────────────────┘
   ↓  path policy      drop node_modules, .next, dist, build, out, coverage,
   ↓                   .git, vendored deps, binaries, media, >256KB files,
   ↓                   lockfile bodies (presence recorded, contents not read)
   ↓  secret filter    .env*, *.pem, *.key, id_rsa*, *.p12, *.keystore,
   ↓                   credentials/service-account json, plus content-level
   ↓                   entropy + known-token-pattern scanning  → QUARANTINE
   ↓  classification   route | component | api-handler | service | model |
   ↓                   config | test | style | asset | unknown
   ↓  parse            tree-sitter (typescript/tsx) → symbols, exports,
   ↓                   imports, JSX usage, fetch/axios call sites
   ↓  graph            module dependency graph; frontend → backend call edges
   ↓  framework detect Next.js version, router style, package manager
   ↓  RepositoryIndex  persisted, queryable, no file bodies
   ↓  retrieval        rank + slice only the snippets a given agent step needs
   ↓
Gemini
```

**Parsing choice:** tree-sitter (`tree-sitter`, `tree-sitter-typescript`) in Python for the baseline index — no Node process, deterministic, fast, tolerant of syntax errors. It gives syntax, not type resolution. If a later ticket proves we need real TypeScript type semantics (cross-file type inference for schema generation), we add an optional `services/analyzer` Node sidecar using `ts-morph` behind the same interface. That sidecar is **not** in MVP scope and must not be assumed.

**Retrieval budget:** every agent step declares a token budget. The retriever fills it by relevance rank and stops. A step that cannot fit its required evidence fails loudly rather than silently truncating.

## 6. Orchestration — deterministic state machine

States:

```
PROJECT_CREATED → REPOSITORY_CONNECTED → ANALYSIS_PENDING → ANALYSIS_RUNNING
→ ANALYSIS_COMPLETE → WORKFLOW_SELECTION_PENDING → WORKFLOWS_SELECTED
→ TOOL_PLAN_RUNNING → TOOL_PLAN_READY → TOOL_PLAN_APPROVAL_PENDING
→ TOOL_PLAN_APPROVED → GENERATION_RUNNING → PATCH_READY
→ SECURITY_REVIEW_RUNNING → (SECURITY_REVIEW_FAILED | SECURITY_REVIEW_PASSED)
→ PATCH_APPROVAL_PENDING → PATCH_APPROVED → VALIDATION_RUNNING
→ (VALIDATION_FAILED | VALIDATION_PASSED) → PR_APPROVAL_PENDING
→ PR_APPROVED → PR_CREATING → PR_CREATED → COMPLETE
```

Rules:
- The legal transition table lives in code as data, and every transition is checked against it. An illegal transition raises; it does not warn.
- Every transition is persisted with actor (`user` | `system` | `agent:<n>`), timestamp, and cause.
- `SECURITY_REVIEW_FAILED` and `VALIDATION_FAILED` route back to `GENERATION_RUNNING` with the findings as input, bounded by a retry limit; on exhaustion the run halts and reports.
- Approval states are gates: the transition out of any `*_APPROVAL_PENDING` state requires a persisted `Approval` record with `status == APPROVED`, an authenticated user id, and a payload hash matching the artifact being approved. Approving a tool plan does not approve the patch that plan produced.

`Approval` record: `{ id, project_id, run_id, gate, artifact_hash, status: PENDING|APPROVED|REJECTED, actor_uid, decided_at }`.

Because the approval carries the artifact hash, a regenerated patch invalidates a prior approval automatically.

## 7. Activity events

The orchestrator emits typed events (`RunEvent`) streamed to the UI over SSE: `step.started`, `step.progress`, `step.completed`, `step.failed`, `approval.requested`, `approval.decided`, `artifact.ready`.

Events carry **task-level summaries only** — "Mapping routes", "Designing WebMCP schemas". Raw model reasoning is never placed in an event. Prompts and full model responses are retained server-side for debugging under the retention policy in `03_SECURITY_ACCESS.md`, never streamed to the client.

## 8. Secure execution

```python
class SecureExecutionProvider(Protocol):
    async def create_workspace(self, spec: WorkspaceSpec) -> Workspace: ...
    async def run(self, ws: Workspace, cmd: Command) -> CommandResult: ...
    async def attestation(self) -> AttestationEvidence | None: ...
    async def destroy(self, ws: Workspace) -> None: ...
```

Implementations:
- `DevelopmentSecureExecutor` — ephemeral temp workspace, non-root subprocess, no network for analysis commands, CPU/memory/wall-clock limits, path jail. `attestation()` returns `None`. Trust level reported as `DEVELOPMENT_ISOLATION`.
- `ConfidentialSpaceSecureExecutor` — Google Confidential Space target. Phase 8. Returns real attestation evidence or nothing.

**Trust levels are an enum with exactly one meaning each:** `DEVELOPMENT_ISOLATION` and `HARDWARE_ATTESTED`. `HARDWARE_ATTESTED` is only ever set by code that has actually verified an attestation token. There is no path that sets it optimistically, and the UI renders the enum, not a boolean.

## 9. GitHub integration

- GitHub **App**, per-repository installation selected by the user. Never `repo`-wide OAuth for the whole account.
- The app's private key lives only in the backend. Installation access tokens are minted per operation and are short-lived.
- Two permission modes on a project: `READ_ONLY` (default) and `WRITE_PR` (granted only by an explicit, recorded user action, and only after the reason is shown).
- Writes: create branch `mcpforge/webmcp-<slug>` from the base commit, commit the patch, open a PR. Never push to the default branch. Never force push. Never rewrite history.
- Every write call asserts the target repository equals the project's bound repository id. A mismatch is a hard error.

## 10. MCPForge's own WebMCP surface

`apps/web/src/webmcp/adapter.ts` wraps the browser API:

- Feature detection: `document.modelContext` per the W3C Web Machine Learning CG draft. Some early implementations expose `navigator.modelContext`; the adapter probes `document` first, then `navigator`, and records which surface it found. Neither present → `unsupported`.
- Registration returns a handle; teardown uses the `AbortSignal` passed in `registerTool` options, matching the draft (which has no `unregisterTool`). React components register on mount and abort on unmount.
- A `MockModelContext` exists for dev and Playwright. It is selected only by explicit opt-in, is labelled `MOCK` in the Trust Panel and in console output, and can never be selected in a production build.
- Tools requiring approval do not act. They create the same `Approval` record the UI uses and return a structured "awaiting human approval" result with the approval id. There is no agent-only fast path.

Because the standard is young and moving, the adapter is the single place that knows the API shape. Spec drift is one file.

## 11. Agent Readiness Score

Computed by deterministic code from the Validator's executed checks. Gemini is never asked to produce a score.

| Component | Points |
|---|---|
| Tool registration & discovery | 20 |
| Schema validity | 15 |
| Successful execution | 25 |
| Error handling (invalid input rejected correctly) | 10 |
| UI state synchronization | 10 |
| Authorization & approval safety | 15 |
| Regression tests still passing | 5 |

Each component stores the check ids and command results that produced it. A component with no evidence scores zero — never a default.

## 12. Configuration

Validated at startup by `config.py` (Pydantic Settings). Missing required variables abort the process with a named error; they never fall back to a default that silently disables security.

See `.env.example` for the full variable list. Secrets exist only in the backend environment. `NEXT_PUBLIC_*` is reserved for genuinely public values (Firebase web config, API base URL) and is reviewed as a security-relevant change.

## 13. Future scope (not MVP)

- Framework adapters: vanilla JS, Vue, Svelte, Angular — behind a `FrameworkAdapter` interface introduced in Phase 5.
- `ts-morph` analyzer sidecar for full type resolution.
- Hosted MCP server / ChatGPT connector exposing `analyze_repository`, `list_workflows`, `generate_webmcp_plan`, `generate_patch`, `run_security_review`, `run_validation`, `create_pull_request`. This is distinct from MCPForge's WebMCP compatibility and is built only after the core product is complete.
- Team accounts and RBAC.
