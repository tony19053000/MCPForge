# MCPForge — Product Requirements Document

**Status:** Phase 0 baseline. Living document — update when product behaviour or scope changes.

---

## 1. Product

**MCPForge**

## 2. One-line product statement

MCPForge is an AI-native developer workspace that analyzes developer-controlled web applications and helps transform them into safe, testable, WebMCP-compatible applications.

## 3. The problem

Almost every web application in existence was designed for a human looking at a screen. An AI browser agent asked to "book a room for two people tomorrow" has to work against that design: read the DOM, guess which element is the date picker, synthesize clicks, wait for a re-render it cannot predict, and infer from pixels whether the action succeeded. This is slow, brittle, and unsafe — the agent cannot tell a search from a purchase.

WebMCP fixes the mechanism. A site declares structured tools directly to the agent via `document.modelContext.registerTool(...)`, and the agent calls `search_hotels({ city, checkIn, guests })` instead of hunting for a button.

But WebMCP moves the difficulty rather than removing it. A developer who wants to adopt it still has to:

- learn a young, moving standard
- decide which of their application's workflows should be exposed at all
- write correct JSON Schemas for each tool
- wire tool handlers into existing business logic without duplicating it
- decide risk boundaries — what is read-only, what mutates state, what is destructive
- implement human approval for anything consequential
- keep the visible UI in sync when an agent acts
- verify that an agent can actually discover and use the tools

That is a week of careful work per application, and most of it is judgment rather than typing. **MCPForge is the tool that does that work with the developer.**

## 4. What MCPForge is not

- Not a browser agent. It does not operate other people's websites.
- Not a hosted proxy that fakes WebMCP support for a site it does not own. The output is real code in the developer's repository.
- Not an autonomous code-modification bot. Every consequential step stops for a human.

## 5. Target users

**Primary**
- Web developers on TypeScript/React/Next.js applications
- AI engineers making an existing product agent-accessible
- SaaS engineering teams and platform teams
- Developer-tool companies

**Secondary**
- Startups preparing an application for the agentic web
- Organizations running exploratory agent-readiness work

## 6. Core user journey

1. Developer signs into MCPForge.
2. Creates or selects a project.
3. Connects a **developer-controlled** GitHub repository through a scoped GitHub App installation.
4. Optionally supplies the deployed application URL.
5. Chooses repository and branch.
6. MCPForge analyzes the repository inside the secure execution boundary, read-only.
7. MCPForge discovers the application's major workflows.
8. Developer selects which workflows AI agents should be allowed to reach.
9. MCPForge designs the WebMCP tools for those workflows.
10. Developer reviews the tool plan — names, schemas, risk class, approval requirements.
11. Developer approves generation.
12. MCPForge generates the integration as a patch.
13. The Security Reviewer agent reviews the generated implementation.
14. Developer reviews findings.
15. The Validator agent tests the WebMCP integration deterministically.
16. MCPForge produces an Agent Readiness Report with a computed score.
17. Developer reviews the exact code diff.
18. Developer approves.
19. MCPForge creates a branch and opens a pull request.
20. Developer merges through their normal GitHub workflow.
21. Once deployed, the website is WebMCP-compatible.

At no point does MCPForge write to the default branch, and at no point does an LLM's output constitute an approval.

## 7. MVP scope

**Target applications:** TypeScript + React + Next.js web applications.

This is a real constraint, stated honestly in the product UI. MCPForge detects the framework and tells the developer plainly when a repository is outside supported scope rather than producing plausible-looking output for a stack it cannot actually reason about.

**MVP delivers:**

- Authentication (starting with the providers that can be configured correctly; others visibly disabled)
- Project workspace with a conversational primary interface
- Scoped GitHub App repository connection, read-only by default
- A bundled **demo project** — a real fixture Next.js application shipped in this repository — so the full pipeline can be exercised without connecting a private repository
- Deterministic repository indexing with secret and path exclusion
- Six-agent runtime pipeline over a deterministic state machine
- Workflow discovery with file/function evidence
- WebMCP tool plan generation with risk classification
- WebMCP integration code generation as a reviewable patch
- Automated security review of generated code
- Deterministic validation and a computed Agent Readiness Score
- Branch + pull request creation
- MCPForge's own WebMCP tool surface
- Development-mode secure execution, labelled as such

## 8. Explicitly out of MVP scope

| Item | Status |
|---|---|
| Vanilla JS, Vue, Svelte, Angular adapters | Future — architecture must allow, MVP must not claim |
| Project upload (zip / drag-and-drop ingestion) | Future — MVP ingests via GitHub App or the bundled demo project only |
| Non-JS backends in the analyzed repository | Future |
| Hardware-attested confidential execution | Interface built in MVP; real attestation is Phase 8 and is marked blocked until real infrastructure exists |
| ChatGPT MCP connector / hosted MCP server | Future scope, documented in `02_ARCHITECTURE.md` §Future |
| Team accounts, org billing, RBAC | Future |
| Automatic merge | Never — the developer merges |

## 9. Framework support policy

MCPForge must never claim support it does not have. Framework support is a table in the product, not a marketing claim:

- **Supported:** analysis, tool design, generation, validation all real.
- **Detected, unsupported:** MCPForge names the framework and declines, explaining what is missing.
- **Unknown:** MCPForge says it could not determine the framework and stops.

## 10. Success criteria for the MVP

1. A real Next.js repository can be connected, analyzed, and produce a workflow list whose evidence points at real files and functions.
2. A generated patch applies cleanly, typechecks and builds in the target repository.
3. The Validator produces an Agent Readiness Score derived only from executed checks, each linked to evidence.
4. A pull request is opened on a branch, never on the default branch.
5. MCPForge's own site exposes working WebMCP tools in a browser that supports the API, with feature detection and no mock presented as real.
6. No secret from a connected repository is ever included in a model prompt — provable by the filtering tests.

## 11. Product principles

- **Human owns every consequential decision.** The AI proposes; deterministic state records; the human decides.
- **Evidence over assertion.** Every discovered workflow, every score component, every security claim points at something verifiable.
- **Honest UI.** Development isolation is never rendered as hardware-backed verification. A mock adapter is never rendered as browser support.
- **Reuse, don't reimplement.** Generated tool handlers call the application's existing business logic.
- **The developer's repository is theirs.** Read-only until explicitly widened, branch-only when widened, PR-only for changes.
