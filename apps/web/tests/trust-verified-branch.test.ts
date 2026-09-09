/**
 * One verified branch, pinned against the source tree — F8-03.
 *
 * `04_FRONTEND_SPEC.md` §8: "The verified style exists in exactly one branch,
 * reachable only when `TrustLevel == HARDWARE_ATTESTED`." A rendering test can
 * only show that the branch behaves for the states it is given; it cannot show
 * that a second branch was not added somewhere else. This reads the TypeScript
 * AST of everything under `src/` and asserts three things:
 *
 * 1. the verified phrase occurs exactly once in the whole tree;
 * 2. exactly one comparison against `HARDWARE_ATTESTED` exists in the tree;
 * 3. that phrase sits inside a branch guarded by that comparison.
 *
 * It is the web-tier equivalent of `F8-01`'s backend sweeps, and it has the
 * same limit, stated rather than papered over: it matches on the literal enum
 * value, so it does not defeat deliberate indirection — a phrase assembled from
 * fragments at runtime, or a guard written through a variable holding the
 * string. What holds regardless of spelling is the rendering test in
 * `trust-panel.test.tsx`, which drives the component with real payload shapes.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "../src");

/** The phrase the spec reserves for a verified execution boundary. */
const VERIFIED_PHRASE = "Hardware-backed Confidential Execution Verified";

/** The enum member that alone may reach it. */
const ATTESTED = "HARDWARE_ATTESTED";

/** The one file allowed to hold either. */
const OWNER = "components/trust/secure-execution-row.tsx";

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry);
    return statSync(full).isDirectory() ? walk(full) : [full];
  });
}

const files = walk(SRC).filter((f) => /\.tsx?$/.test(f));

interface Hit {
  file: string;
  node: ts.Node;
  source: ts.SourceFile;
  parents: Map<ts.Node, ts.Node>;
}

const parsed = new Map<string, { source: ts.SourceFile; parents: Map<ts.Node, ts.Node> }>();

/**
 * Parsed once per file and cached, so every sweep below walks the *same* node
 * objects. Re-parsing would give each sweep its own tree and make the identity
 * check in the guard test compare nodes that could never be equal — it would
 * then fail for a reason that has nothing to do with the property.
 */
function parse(file: string): { source: ts.SourceFile; parents: Map<ts.Node, ts.Node> } {
  const cached = parsed.get(file);
  if (cached) return cached;

  const source = ts.createSourceFile(
    file,
    readFileSync(file, "utf8"),
    ts.ScriptTarget.ESNext,
    true,
    ts.ScriptKind.TSX,
  );
  const parents = new Map<ts.Node, ts.Node>();
  const link = (node: ts.Node) => {
    node.forEachChild((child) => {
      parents.set(child, node);
      link(child);
    });
  };
  link(source);
  const result = { source, parents };
  parsed.set(file, result);
  return result;
}

/** Every node whose own text carries the phrase — string, template or JSX text. */
function phraseHits(): Hit[] {
  const hits: Hit[] = [];
  for (const file of files) {
    const { source, parents } = parse(file);
    const visit = (node: ts.Node) => {
      const text =
        ts.isStringLiteralLike(node) || ts.isJsxText(node) ? node.text : undefined;
      if (text?.includes(VERIFIED_PHRASE)) {
        hits.push({ file: path.relative(SRC, file), node, source, parents });
      }
      node.forEachChild(visit);
    };
    visit(source);
  }
  return hits;
}

/** Every comparison whose operand is the attested enum value, written literally. */
function attestedComparisons(): Hit[] {
  const operators = new Set<ts.SyntaxKind>([
    ts.SyntaxKind.EqualsEqualsEqualsToken,
    ts.SyntaxKind.EqualsEqualsToken,
    ts.SyntaxKind.ExclamationEqualsEqualsToken,
    ts.SyntaxKind.ExclamationEqualsToken,
  ]);
  const hits: Hit[] = [];
  for (const file of files) {
    const { source, parents } = parse(file);
    const visit = (node: ts.Node) => {
      if (ts.isBinaryExpression(node) && operators.has(node.operatorToken.kind)) {
        const sides = [node.left, node.right];
        if (sides.some((s) => ts.isStringLiteralLike(s) && s.text === ATTESTED)) {
          hits.push({ file: path.relative(SRC, file), node, source, parents });
        }
      }
      node.forEachChild(visit);
    };
    visit(source);
  }
  return hits;
}

/** Every mention of the enum value anywhere in the tree, comparison or not. */
function attestedMentions(): { file: string; line: number }[] {
  const found: { file: string; line: number }[] = [];
  for (const file of files) {
    const { source } = parse(file);
    const visit = (node: ts.Node) => {
      const isMention =
        (ts.isStringLiteralLike(node) && node.text === ATTESTED) ||
        (ts.isIdentifier(node) && node.text === ATTESTED);
      if (isMention) {
        found.push({
          file: path.relative(SRC, file),
          line: source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1,
        });
      }
      node.forEachChild(visit);
    };
    visit(source);
  }
  return found;
}

function ancestors(hit: Hit): ts.Node[] {
  const chain: ts.Node[] = [];
  let current = hit.parents.get(hit.node);
  while (current) {
    chain.push(current);
    current = hit.parents.get(current);
  }
  return chain;
}

describe("the verified execution branch", () => {
  it("scans a real tree, so an empty sweep cannot pass", () => {
    expect(files.length).toBeGreaterThan(20);
    expect(files.some((f) => f.endsWith(OWNER))).toBe(true);
  });

  it("holds the verified phrase in exactly one place", () => {
    const hits = phraseHits();
    expect(hits.map((h) => h.file)).toEqual([OWNER]);
  });

  it("compares against the attested level in exactly one place", () => {
    // A second guard is a second branch, however it renders. The comparison is
    // the thing being limited, not the wording.
    const comparisons = attestedComparisons();
    expect(comparisons.map((c) => c.file)).toEqual([OWNER]);

    // And the value is not named anywhere else in the tree either — not in a
    // constant, a lookup table or a type-narrowing helper. `types.ts` declares
    // the union, which is the one other legitimate mention.
    const mentions = attestedMentions();
    const outside = mentions.filter((m) => m.file !== OWNER && m.file !== "lib/api/types.ts");
    expect(outside).toEqual([]);
  });

  it("renders the phrase only inside the branch that comparison guards", () => {
    const [hit] = phraseHits();
    expect(hit).toBeDefined();
    if (!hit) return;

    const [comparison] = attestedComparisons();
    expect(comparison).toBeDefined();
    if (!comparison) return;

    // Walk out from the phrase. A branching construct must be found whose
    // *condition* contains that same comparison node — so the phrase being in
    // the same file, or in a sibling branch, is not enough.
    const guards = ancestors(hit).filter(
      (node) => ts.isIfStatement(node) || ts.isConditionalExpression(node),
    );
    const guarded = guards.some((node) => {
      const condition = ts.isIfStatement(node) ? node.expression : node.condition;
      let contains = false;
      const visit = (n: ts.Node) => {
        if (n === comparison.node) contains = true;
        n.forEachChild(visit);
      };
      visit(condition);
      return contains;
    });

    expect(guarded).toBe(true);
  });

  it("guards on the trust level itself, not on a prop a caller can set", () => {
    const [comparison] = attestedComparisons();
    expect(comparison).toBeDefined();
    if (!comparison) return;

    const expression = comparison.node as ts.BinaryExpression;
    const subject = [expression.left, expression.right].find(
      (side) => !ts.isStringLiteralLike(side),
    );
    expect(subject).toBeDefined();
    // `execution.trust_level` — the field the server sends, reached through the
    // payload object. A bare boolean prop would not match.
    expect(subject?.getText(comparison.source)).toMatch(/\.trust_level$/);
  });
});
