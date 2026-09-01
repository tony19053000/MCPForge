# CLAUDE.md — MCPForge

Read this first, every session.

## 1. Read before doing anything

1. `STATUS.md` — where the project actually is. Start here.
2. `01_PRD.md` — product and scope
3. `02_ARCHITECTURE.md` — stack, layout, orchestration
4. `03_SECURITY_ACCESS.md` — **binding** security rules
5. `04_FRONTEND_SPEC.md` — UI language and behaviour
6. `05_FEATURE_TICKETS.md` — the unit of work

## 2. What MCPForge is

An AI-native developer workspace that analyzes a developer's own web application and helps transform it into a safe, testable, WebMCP-compatible application. MVP target: TypeScript + React + Next.js.

## 3. Stack (do not drift)

- **Web:** `apps/web` — Next.js App Router, React, TypeScript strict, Tailwind. Hosts MCPForge's own WebMCP surface.
- **Backend:** `services/api` — Python 3.12, FastAPI, Pydantic v2, `uv`. All AI, credentials, repository access and authorization live here.
- **LLM:** `google-genai` Python SDK only. Model id from `GEMINI_MODEL`, never a literal.
- **No agent framework.** LangChain, CrewAI, AutoGen, LlamaIndex are banned in V1.
- **WebMCP:** the real browser API (`document.modelContext.registerTool`, with a `navigator.modelContext` fallback probe) behind `apps/web/src/webmcp/adapter.ts`.

Python on the backend is the project owner's stated preference and a deliberate decision — see `02_ARCHITECTURE.md` §1.1. Do not "unify" the stack to TypeScript.

## 4. Development roles

Two Claude Code subagents in `.claude/agents/`:

- **`coder`** — `[CODER]` implements approved tickets. May never declare a phase or ticket complete.
- **`reviewer-tester`** — `[REVIEWER / TESTER]` independently reviews and tests. Returns `PASS` or `FAIL` with exact reasons.

## 5. The review gate — never skip it

```
[CODER] implements → [REVIEWER / TESTER] reviews
   ↓ FAIL → [CODER] fixes → [REVIEWER / TESTER] retests
   ↓ PASS
update documentation → update STATUS.md → commit → push → next phase
```

Completion percentage advances only on a `PASS`. Writing code is not progress.

## 6. Non-negotiables

1. Never push generated customer changes to a default or protected branch. Branch + PR only. Never force push.
2. Never expose the Gemini key, GitHub App private key, or Firebase Admin credentials to the browser. Never commit `.env`.
3. Never send secrets or excluded paths into a prompt. Filtering runs **before** context construction.
4. Never treat model output as authorization. Gates read `Approval` records, not agent text.
5. Never claim `HARDWARE_ATTESTED` / TEE verification without a real verified attestation. Development isolation is labelled as development isolation.
6. Never present a mock (WebMCP adapter, integration, test result) as real capability.
7. Never display raw model chain-of-thought.
8. Never dump a repository into Gemini. `Repository → deterministic index → relevant context → Gemini`.
9. Never fake a test result or hardcode a value to make a check pass.
10. Never change product scope silently. Scope changes update `01_PRD.md` in the same commit.

## 7. After every completed phase

Update `STATUS.md`: percentage, phase, tickets, tests, security state, latest commit, and an appended **Context State Log** entry (what was built, decisions made, files introduced, open issues, what not to touch, next task). Never delete previous log entries.

## 8. Verification commands

```bash
npm run typecheck   # web + api (mypy)
npm run lint        # web + api (ruff)
npm run test        # web + api (pytest)
npm run build       # web only — Python is not a build step
npm run test:e2e    # once Playwright exists
```

`typecheck`, `lint` and `test` fan out to both stacks from the repository root. `build` applies to the web tier only; the backend's equivalent gate is `typecheck` + `test`.

## 9. Blockers

External access you do not have (GCP Confidential Space, an unconfigured auth provider, a missing API key) is recorded as a blocker in `STATUS.md` and the work continues elsewhere. Never invent credentials, never simulate an integration to clear a blocker, never lower a security requirement to get past one.
