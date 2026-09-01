# MCPForge

**An AI-native developer workspace that analyzes developer-controlled web applications and helps transform them into safe, testable, WebMCP-compatible applications.**

> **Project status: Phase 1 of 9 — application foundation.**
> Both tiers now build, typecheck, lint and test cleanly, with authentication, environment validation, the design system and the workspace shell in place. **The pipeline below is not implemented yet** — repository analysis, tool generation, security review and pull-request creation all arrive in Phases 3–6. Nothing here describes a working feature unless `STATUS.md` says it is done.

---

## The problem

Web applications were built for humans looking at screens. An AI agent asked to "book a room for two tomorrow" has to read the DOM, guess which element is the date picker, synthesize clicks, and infer from pixels whether it worked. It cannot reliably tell a search from a purchase.

[WebMCP](https://webmachinelearning.github.io/webmcp/) fixes the mechanism: a site declares structured tools directly to the agent via `document.modelContext.registerTool(...)`, so the agent calls `search_hotels({ city, checkIn, guests })` instead of hunting for a button.

But adopting it is still a week of careful judgment per application — deciding which workflows to expose, writing schemas, wiring handlers into existing business logic without duplicating it, classifying risk, gating anything consequential behind human approval, and proving an agent can actually use the result.

**MCPForge does that work with the developer.**

## How it works

```
Connect repository (scoped, read-only)
   → deterministic index (secrets and build output filtered out)
   → Codebase Analyst        discovers structure and workflows
   → you choose              which workflows agents may reach
   → Workflow Architect      designs intent-level WebMCP tools
   → you approve             the tool plan
   → WebMCP Generator        writes the integration as a patch
   → Security Reviewer       reviews the generated code
   → Validator               runs real checks, computes a readiness score
   → you approve             the diff
   → branch + pull request   you merge
```

Six runtime agents, one Gemini provider, and a deterministic state machine around them. The AI proposes; deterministic code records and enforces; the human decides.

MCPForge is also **itself** WebMCP-compatible — an agent can drive MCPForge through the same approval gates a human uses.

## Stack

| Tier | Stack |
|---|---|
| Web — `apps/web` | Next.js (App Router), React, TypeScript, Tailwind. Hosts MCPForge's own WebMCP surface. |
| Backend — `services/api` | Python 3.12, FastAPI, Pydantic v2, `uv`. All AI calls, credentials, repository access and authorization. |
| Model | Gemini via the official `google-genai` Python SDK. Model id from `GEMINI_MODEL`. |
| Auth | Firebase Authentication (separate from GitHub repository authorization) |
| Repository access | GitHub App, per-repository installation, read-only by default |

No agent orchestration framework. The orchestration layer is ours and it is deterministic.

## Principles

- **Human owns every consequential decision.** Model output is never authorization.
- **Evidence over assertion.** Every workflow, score component and security claim points at something verifiable.
- **Honest UI.** Development isolation is never rendered as hardware-backed attestation. A mock is never rendered as real support.
- **Reuse, don't reimplement.** Generated tool handlers call the application's existing business logic.
- **Your repository is yours.** Read-only until you widen it, branch-only when you do, pull request for every change.

## Getting started

```bash
cp .env.example .env     # fill in real values; never commit .env
npm install
npm run dev              # web  → http://localhost:3000
npm run api:dev          # api  → http://localhost:8000
```

Server-side Google access uses Application Default Credentials, so run
`gcloud auth application-default login` once. There are no service-account key
files, and none are supported.

Verification, all of which must pass before anything is committed:

```bash
npm run typecheck   # web tsc + api mypy (strict)
npm run lint        # eslint + ruff
npm run test        # vitest + pytest
npm run build       # web only — Python has no build step
```

## Documentation

| Document | Contents |
|---|---|
| [`01_PRD.md`](01_PRD.md) | Product, users, journey, MVP scope |
| [`02_ARCHITECTURE.md`](02_ARCHITECTURE.md) | Stack, layout, agents, orchestration, indexing |
| [`03_SECURITY_ACCESS.md`](03_SECURITY_ACCESS.md) | Threat model and binding security rules |
| [`04_FRONTEND_SPEC.md`](04_FRONTEND_SPEC.md) | Visual language, layout, approval and trust UI |
| [`05_FEATURE_TICKETS.md`](05_FEATURE_TICKETS.md) | Every ticket, phase by phase |
| [`STATUS.md`](STATUS.md) | Real current state. Always accurate. |
| [`CLAUDE.md`](CLAUDE.md) | Engineering process for AI sessions |

## License

MIT
