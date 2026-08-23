import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

/**
 * D-82 contract regression for the Vercel Web Analytics integration.
 *
 * On the parser choice, because the obvious one is not available here. The
 * repository pins typescript 7.0.2, whose only stable export is the version
 * string: the AST lives behind `typescript/unstable/ast`, and the export name
 * is the maintainers saying not to depend on it. A named CI gate that breaks on
 * an upstream refactor of an explicitly unstable API is worse than no gate,
 * and adding a second parser would add a framework this project does not
 * otherwise need. So this reads source text.
 *
 * Naive grep over raw source fails in three specific ways, and all three are
 * handled below by stripping comments and string literals before matching.
 * Without that, a mention of <Analytics /> inside a comment like this one
 * counts as a mount, a second real mount is invisible to a boolean match, and
 * an import written inside a string passes as the real thing.
 *
 * The failures this protects against are all silent. Two mounts double every
 * page view without erroring. Importing from "@vercel/analytics" rather than
 * "@vercel/analytics/next" builds cleanly and then misses App Router
 * navigations. A "use client" directive added to the root layout for unrelated
 * reasons converts the whole tree to a client component, a large regression no
 * analytics assertion would otherwise notice.
 *
 * What it deliberately does not claim: that the page view reaches Vercel. A
 * passing build proves integration, never ingestion. See D-82.
 */

const layoutSource = await readFile(
  new URL("../app/layout.tsx", import.meta.url),
  "utf8",
);
const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);

/**
 * Blank out comments, and optionally the contents of string and template
 * literals, so that a match is against code rather than prose. Replacements are
 * spaces of equal length with newlines preserved, so every offset stays valid
 * for the <body> containment check.
 *
 * Two passes exist because the two questions need different inputs. Counting
 * JSX mounts must not see inside literals, or a template literal containing the
 * text of a component counts as a mount. Matching the import must still see
 * inside literals, because the module specifier IS a string.
 */
function blankOut(source, { literals }) {
  let out = "";
  let index = 0;
  const blank = (text) => text.replace(/[^\n]/g, " ");

  while (index < source.length) {
    const two = source.slice(index, index + 2);

    if (two === "/*") {
      const end = source.indexOf("*/", index + 2);
      const stop = end === -1 ? source.length : end + 2;
      out += blank(source.slice(index, stop));
      index = stop;
      continue;
    }

    if (two === "//") {
      const end = source.indexOf("\n", index);
      const stop = end === -1 ? source.length : end;
      out += blank(source.slice(index, stop));
      index = stop;
      continue;
    }

    const quote = source[index];
    if (quote === '"' || quote === "'" || quote === "`") {
      let cursor = index + 1;
      while (cursor < source.length) {
        if (source[cursor] === "\\") {
          cursor += 2;
          continue;
        }
        if (source[cursor] === quote) break;
        cursor += 1;
      }
      const stop = Math.min(cursor + 1, source.length);
      if (literals === "blank") {
        // Keep the quote characters, blank what is between them.
        out += quote + blank(source.slice(index + 1, stop - 1)) + quote;
      } else {
        out += source.slice(index, stop);
      }
      index = stop;
      continue;
    }

    out += source[index];
    index += 1;
  }

  return out;
}

// For JSX structure: literals blanked, so a decoy inside a template literal
// cannot masquerade as a mount. An earlier version of this file kept literal
// contents, and a `{`<Analytics />`}` decoy with the real mount deleted passed
// all four assertions. That is a gate reporting green over a broken contract,
// which is worse than having no gate.
const structure = blankOut(layoutSource, { literals: "blank" });
// For the import: literals kept, because the module specifier is a string.
const statements = blankOut(layoutSource, { literals: "keep" });

test("Analytics is a production dependency, exact-pinned, at major 2 or later", () => {
  const pinned = packageJson.dependencies?.["@vercel/analytics"];
  assert.ok(pinned, "@vercel/analytics must be a production dependency");
  assert.equal(
    packageJson.devDependencies?.["@vercel/analytics"],
    undefined,
    "@vercel/analytics must not also be a devDependency",
  );

  // Only assert the exact-pin convention while the repository still follows it
  // everywhere else. Asserting it unconditionally would turn a deliberate
  // project-wide policy change into a failure in this one unrelated test.
  //
  // The exclusion of @vercel/analytics itself is the whole point. An earlier
  // version tested every dependency including this one, which made the guard
  // self-defeating: loosening this pin to "2.x" also made the guard false, so
  // the assertion that exists to catch exactly that mutation was skipped
  // instead of failing. Found by mutating the pin and watching the gate stay
  // green.
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
  // Locate import STATEMENTS in the literal-blanked source, so a line inside a
  // multiline template literal cannot pose as one. Anchoring at line start is
  // not enough on its own: with the `m` flag, every line of a template literal
  // begins a line too, and an earlier version of this test accepted
  //
  //     const decoy = `
  //     import { Analytics } from "@vercel/analytics/next";
  //     `;
  //
  // as the real import while the real import was deleted. Blanking preserves
  // offsets, so the specifier is recovered from the original source at the same
  // span, which is what makes matching on the blanked copy safe here.
  const statementPattern = /^import\s*\{([^}]*)\}\s*from\s*["'][^"']*["']/gm;
  const found = [];
  for (const match of structure.matchAll(statementPattern)) {
    const names = match[1].split(",").map((name) => name.trim());
    if (!names.includes("Analytics")) continue;
    const real = layoutSource.slice(match.index, match.index + match[0].length);
    const specifier = /from\s*["']([^"']*)["']/.exec(real)?.[1] ?? "";
    if (!specifier.startsWith("@vercel/analytics")) continue;
    found.push(specifier.slice("@vercel/analytics".length));
  }

  assert.equal(
    found.length,
    1,
    `the root layout must import Analytics exactly once, found ${found.length}`,
  );
  assert.equal(
    found[0],
    "/next",
    'Analytics must come from "@vercel/analytics/next"; the bare package entry ' +
      "builds cleanly and then misses App Router navigations",
  );
});

test("exactly one Analytics element is mounted, and it is under <body>", () => {
  const mounts = [...structure.matchAll(/<Analytics(\s|\/|>)/g)];
  assert.equal(
    mounts.length,
    1,
    `exactly one <Analytics /> must be mounted, found ${mounts.length}; a ` +
      "second mount double-counts every page view without erroring",
  );

  const bodyOpen = structure.indexOf("<body");
  const bodyClose = structure.indexOf("</body>");
  assert.ok(bodyOpen !== -1 && bodyClose !== -1, "the layout must render <body>");
  assert.ok(
    mounts[0].index > bodyOpen && mounts[0].index < bodyClose,
    "<Analytics /> must be mounted beneath <body>",
  );
});

test("the root layout was not converted to a client component", () => {
  // Run against the comment-stripped source, because a comment does not stop a
  // directive from being a directive. An earlier version took the first
  // non-blank line of the raw source, so
  //
  //     // innocent leading comment
  //     "use client";
  //
  // hid the directive behind the comment and the assertion passed. Comments are
  // blanked to whitespace here, so leading trivia cannot conceal it. Not
  // multiline: the prologue is only at the start of the module.
  const hasUseClientDirective = /^\s*["']use client["']\s*;?/.test(statements);

  assert.equal(
    hasUseClientDirective,
    false,
    'the root layout must stay a server component; "@vercel/analytics/next" ' +
      'is the maintained integration and needs no "use client" here',
  );
});
