# MCPForge — Frontend Specification

**Status:** Phase 0 baseline. Living document.

MCPForge should feel like a premium modern AI developer environment — the calm, dense, confident feel of a professional tool, not a marketing site with a chat box bolted on. Influences: ChatGPT, Claude, modern AI coding agents. **Original identity — no pixel-copying of any existing product.**

---

## 1. Identity

**Tone:** precise, quiet, trustworthy. This product touches private source code and opens pull requests; the interface should feel like it takes that seriously.

**Design direction**
- Dark-first, with a fully supported light theme. Neither is an afterthought.
- Near-neutral ground (not pure black / pure white) with one restrained accent used for action and progress. Forge-warm accent — amber/ember — against cool neutrals.
- Semantic colors reserved for meaning: success, warning, danger, and a distinct "awaiting you" state for approvals.
- Type: one modern sans for UI, one mono for code, paths, schemas and diffs.
- Generous vertical rhythm; dense where the data is dense (diffs, indexes) and airy where the human is deciding.
- Motion is functional: state transitions, streaming text, progress. No decorative animation.

**Accessibility is a requirement:** WCAG AA contrast in both themes, visible focus rings, full keyboard operability on every approval control, `prefers-reduced-motion` respected, status changes announced to screen readers. Never color alone to convey risk class or security state.

## 2. Layout

Three regions on desktop.

```
┌────────────┬───────────────────────────────┬─────────────────────┐
│  SIDEBAR   │        WORKSPACE              │   CONTEXT PANEL     │
│  260px     │        flexible               │   360–480px         │
│            │                               │                     │
│  logo      │  conversation + activity      │  contextual to the  │
│  new proj  │  timeline, interleaved        │  current run state  │
│  projects  │                               │                     │
│  repo      │  ...                          │                     │
│  sessions  │  ┌─────────────────────────┐  │                     │
│  settings  │  │ composer                │  │                     │
└────────────┴──┴─────────────────────────┴──┴─────────────────────┘
```

### 2.1 Left sidebar
MCPForge logo · New project · Recent projects · Connected repository (with access mode badge) · Sessions / history · Settings. Collapsible to icons.

### 2.2 Center workspace
One continuous intelligent session. The developer types:

> Make my booking workflow WebMCP compatible.

and the session proceeds through visible states. Conversation turns, activity groups, artifact cards and approval cards share a single chronological column — the run *is* the conversation, not a sidebar to it.

### 2.3 Right context panel
Contents follow run state: project info · repository explorer · workflow discoveries · WebMCP tool definitions · security report · code diff · validation report · trust panel. Tabbed, with the tab relevant to the current state auto-selected but never stealing a tab the user chose.

## 3. Activity visualization

Grouped, high-level steps with live status:

```
Scanning repository            ✓  1,284 files · 312 indexed
Mapping routes                 ✓  47 routes
Detecting business functions   ✓  126 functions
Identifying workflows          ✓  7 candidates
Designing WebMCP schemas       ◐  running
Generating integration         ·  pending
Running security review        ·  pending
Testing tool discovery         ·  pending
Preparing pull request         ·  pending
```

**Hard rule: no raw chain-of-thought.** Display task summaries, counts, file paths, and verifiable actions. Never stream internal model reasoning to the user. Steps expand to show *evidence* — files touched, commands run, exit codes — not model deliberation.

## 4. Approval UI

Consequential steps stop and wait. An approval card is visually distinct (accent border, "Awaiting your decision" label), is announced to assistive tech, and cannot be dismissed by clicking away.

Actions: **Approve** · **Modify** · **Reject** — or, where a selection is being confirmed: **Continue** · **Edit selection** · **Cancel**.

Requirements:
- The card states exactly what approving permits, in one sentence.
- The card shows the artifact it covers and its hash-derived version; if the artifact is regenerated the card invalidates itself and says so.
- Destructive items require typed or explicitly re-confirmed intent.
- The button reflects real server state. The UI never optimistically renders "approved" before the store confirms it.

## 5. Loading and streaming states

Skeletons that match final layout (no layout shift), a determinate progress bar where a real count exists, indeterminate shimmer only where it does not. Streaming text renders token-by-token with a stable cursor. Long steps show elapsed time and a cancel affordance where cancellation is genuinely supported — and no cancel button where it is not.

## 6. Code diff view

Per file: path · language · additions/removals count · unified or split view · syntax highlighting · collapsed unchanged regions.

Each file carries:
- **Why this file changes** — one sentence, plain language.
- **Affected WebMCP tool** — chip linking to the tool definition.

Above the file list: total files, total additions/removals, target branch name, and the base commit.

## 7. Workflow cards

Selectable cards during workflow selection. Risk class shown as a labelled chip — text plus color plus icon, never color alone.

```
Search hotels                Check room availability
READ · Safe                  READ · Safe

Prepare booking              Cancel reservation
WRITE · Human approval       DESTRUCTIVE · Human approval
```

Each card expands to show the evidence: the files and functions the analyst mapped it to. A workflow with weak evidence is marked "low confidence" and is not preselected.

## 8. Trust panel

Renders real state only, read from the server:

```
Repository boundary      acme/hotel-app  ·  branch: main
Access mode              READ_ONLY
Secret filtering         Active · 3 files quarantined
Secure execution         Development Isolation
                         Not hardware-attested
Branch protection        Writes restricted to mcpforge/* branches
WebMCP adapter           document.modelContext · supported
```

Rules:
- No green check for a state that has not been verified.
- "Development Isolation" is rendered in a neutral/informational style with the explicit "Not hardware-attested" line. The verified style exists in exactly one branch, reachable only when `TrustLevel == HARDWARE_ATTESTED`.
- Quarantined-file count links to the list of paths (paths only, never contents).
- When the browser lacks WebMCP, the panel says "not supported in this browser". When the mock adapter is active it says **MOCK ADAPTER — not real browser WebMCP** in a warning style.

## 9. Agent readiness report

```
Agent Readiness
96 / 100

Tool discovery         PASS   20/20   4 tools registered and discovered
Schema validation      PASS   15/15   4/4 schemas valid
Execution              PASS   25/25   4/4 tools executed successfully
Error handling          —      6/10   2/4 tools rejected invalid input
UI synchronization     PASS   10/10   DOM state updated after each call
Security boundaries    PASS   15/15   2 gated tools blocked without approval
Regression             PASS    5/5    existing suite green
```

Every row links to its evidence: the check ids, the commands run, and their output. The total is computed by code from these rows. Gemini never produces the score, and a row with no evidence scores zero rather than defaulting.

## 10. Empty, error and unsupported states

- **No project:** a single clear primary action, plus the two connection paths that exist in the MVP — **GitHub repository** (scoped App installation) and **Demo project** (the bundled fixture application). Project upload is future scope and must not appear as a disabled-looking option that implies it is coming imminently; it appears only if and when it is built.
- **Unsupported framework:** name the detected framework, state plainly that MCPForge supports Next.js/React/TypeScript today, and stop. Never proceed with degraded output.
- **Run failure:** what failed, at which step, the real error, and the available next actions. Never a generic "something went wrong".
- **Error boundaries:** per-region, so a failing context panel does not take down the session.

## 11. Responsive behaviour

- **Desktop (≥1280px):** primary. Full three-region layout.
- **Tablet (768–1279px):** sidebar collapses to icons; context panel becomes an overlay drawer. Must remain fully usable, including approvals.
- **Mobile (<768px):** simplified monitoring and chat — read the session, follow activity, respond to approvals. Diff review and workflow selection may direct the user to a larger screen rather than degrading into an unusable view.

## 12. MCPForge's own WebMCP surface in the UI

MCPForge is itself an agent-accessible application. The UI must make that visible and honest: a panel listing MCPForge's registered tools, their schemas, risk classes and approval requirements, and whether the current browser supports the API. When an AI agent invokes a MCPForge tool, the resulting activity appears in the same timeline as human-initiated work, labelled with its origin — so the developer always sees what an agent did on their behalf.
