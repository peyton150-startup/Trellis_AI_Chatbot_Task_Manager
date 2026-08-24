import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import tsNamespace from "@typescript/typescript6";

/**
 * D-82 contract regression for the Vercel Web Analytics integration.
 *
 * This parses the root layout into a real AST. An earlier version of this file
 * did not, and the history is the argument for why it now does.
 *
 * The repository compiles with typescript 7.0.2, whose only stable export is a
 * version string: its AST sits behind `typescript/unstable/ast`, and that name
 * is the maintainers saying not to depend on it. The first attempt at this gate
 * used it anyway, failed to load, and was replaced with a hand-written lexer
 * that blanked comments and string literals before matching text.
 *
 * That lexer was wrong six times, and each hole was found by attacking the gate
 * rather than running it. Every one let the gate report green while no
 * Analytics component rendered at all:
 *
 *   1. a mention inside a comment counted as a mount
 *   2. a `{`<Analytics />`}` template literal counted as a mount
 *   3. an import inside a multiline template literal counted as the real import
 *   4. `"use client"` hidden behind a leading comment escaped the check
 *   5. `{false && <Analytics />}` satisfied every textual assertion, and
 *      compiled under `next build`, while never rendering
 *   6. `<body data-decoy={/<Analytics \/>/.source}>` hid a mount inside a
 *      regular-expression literal, which the lexer did not model, and its `>`
 *      was even mistaken for the end of the <body> opening tag
 *
 * Each fix closed one lexical case and the next case arrived. JavaScript has
 * more of them than a test should own: nested templates, escapes, division
 * versus regex ambiguity. `@typescript/typescript6` is Microsoft's supported
 * side-by-side parser for exactly this situation, published so TS7 projects can
 * still use the TS6 programmatic API. Used here for the test only; the
 * application still compiles with typescript 7.0.2.
 *
 * What it deliberately does not claim: that a page view reaches Vercel. A
 * passing build proves integration, never ingestion. See D-82.
 */

const ts = tsNamespace.default ?? tsNamespace;

const layoutSource = await readFile(
  new URL("../app/layout.tsx", import.meta.url),
  "utf8",
);
const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);

const sourceFile = ts.createSourceFile(
  "layout.tsx",
  layoutSource,
  ts.ScriptTarget.Latest,
  /* setParentNodes */ true,
  ts.ScriptKind.TSX,
);

function walk(node, visit) {
  visit(node);
  ts.forEachChild(node, (child) => walk(child, visit));
}

function openingElementOf(node) {
  if (ts.isJsxSelfClosingElement(node)) return node;
  if (ts.isJsxElement(node)) return node.openingElement;
  return undefined;
}

function tagNameOf(node) {
  const opening = openingElementOf(node);
  return opening ? opening.tagName.getText(sourceFile) : undefined;
}

test("Analytics is a production dependency, exact-pinned, at major 2 or later", () => {
  const pinned = packageJson.dependencies?.["@vercel/analytics"];
  assert.ok(pinned, "@vercel/analytics must be a production dependency");
  assert.equal(
    packageJson.devDependencies?.["@vercel/analytics"],
    undefined,
    "@vercel/analytics must not also be a devDependency",
  );

  // Only assert the exact-pin convention while the repository still follows it
  // everywhere else, so a deliberate project-wide policy change does not fail
  // here. The package under test is excluded from that sample on purpose: an
  // earlier version included it, which made the guard self-defeating, because
  // loosening this pin to "2.x" also made the guard false and skipped the
  // assertion written to catch exactly that.
  const everyOtherDependencyIsPinned = Object.entries(packageJson.dependencies)
    .filter(([name]) => name !== "@vercel/analytics")
    .every(([, range]) => /^\d+\.\d+\.\d+$/.test(range));
  if (everyOtherDependencyIsPinned) {
    assert.match(
      pinned,
      /^\d+\.\d+\.\d+$/,
      "@vercel/analytics must be exact-pinned like every other dependency",
    );
  }

  // Resilient Intake is the reason D-82 requires v2 rather than v1.
  const major = Number.parseInt(pinned, 10);
  assert.ok(
    Number.isInteger(major) && major >= 2,
    `@vercel/analytics must be major 2 or later, found ${pinned}`,
  );
});

test("Analytics is imported as a named import from the Next.js entry point", () => {
  const imports = [];
  walk(sourceFile, (node) => {
    if (!ts.isImportDeclaration(node)) return;
    const specifier = node.moduleSpecifier;
    if (!ts.isStringLiteral(specifier)) return;
    const bindings = node.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) return;
    for (const element of bindings.elements) {
      if (element.name.text === "Analytics") imports.push(specifier.text);
    }
  });

  assert.equal(
    imports.length,
    1,
    `the root layout must import Analytics exactly once, found ${imports.length}`,
  );
  assert.equal(
    imports[0],
    "@vercel/analytics/next",
    'Analytics must come from "@vercel/analytics/next"; the bare package entry ' +
      "builds cleanly and then misses App Router navigations",
  );
});

test("exactly one Analytics element is mounted, unconditionally, under <body>", () => {
  const mounts = [];
  walk(sourceFile, (node) => {
    if (tagNameOf(node) === "Analytics") mounts.push(node);
  });

  assert.equal(
    mounts.length,
    1,
    `exactly one <Analytics /> must be mounted, found ${mounts.length}; a ` +
      "second mount double-counts every page view without erroring",
  );

  // Walk up to <body>, and reject any JavaScript expression on the way. Being
  // inside <body> is not the same as rendering: a conditional mount compiles
  // and reads correctly while never appearing. A JSX child sits directly in its
  // parent element; anything wrapped in a conditional, a logical operator, or a
  // call is the value of an expression instead, which is what this rejects.
  let node = mounts[0].parent;
  let reachedBody = false;
  while (node) {
    if (
      ts.isConditionalExpression(node) ||
      ts.isBinaryExpression(node) ||
      ts.isCallExpression(node) ||
      ts.isArrowFunction(node)
    ) {
      assert.fail(
        "<Analytics /> must be mounted unconditionally; it is inside a " +
          `${ts.SyntaxKind[node.kind]}, which compiles and passes every ` +
          "textual check while it may never render",
      );
    }
    if (tagNameOf(node) === "body") {
      reachedBody = true;
      break;
    }
    node = node.parent;
  }

  assert.ok(reachedBody, "<Analytics /> must be mounted beneath <body>");
});

test("the root layout was not converted to a client component", () => {
  // A directive is a string-literal expression statement in the prologue, so
  // leading comments cannot conceal it and a matching string elsewhere in the
  // file cannot fake it.
  const directives = [];
  for (const statement of sourceFile.statements) {
    if (!ts.isExpressionStatement(statement)) break;
    if (!ts.isStringLiteral(statement.expression)) break;
    directives.push(statement.expression.text);
  }

  assert.equal(
    directives.includes("use client"),
    false,
    'the root layout must stay a server component; "@vercel/analytics/next" ' +
      'is the maintained integration and needs no "use client" here',
  );
});
