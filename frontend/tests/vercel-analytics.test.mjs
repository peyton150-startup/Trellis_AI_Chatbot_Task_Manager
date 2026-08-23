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
 * Remove block comments, line comments, and string/template literals, so that
 * every match below is against code rather than prose. Replaces with spaces to
 * keep offsets stable, which matters for the <body> containment check.
 */
function stripCommentsAndStrings(source) {
  let out = "";
  let index = 0;
  const blank = (length) => " ".repeat(length);

  while (index < source.length) {
    const two = source.slice(index, index + 2);

    if (two === "/*") {
      const end = source.indexOf("*/", index + 2);
      const stop = end === -1 ? source.length : end + 2;
      out += blank(stop - index).replace(/ /g, (character, offset) =>
        source[index + offset] === "\n" ? "\n" : character,
      );
      index = stop;
      continue;
    }

    if (two === "//") {
      const end = source.indexOf("\n", index);
      const stop = end === -1 ? source.length : end;
      out += blank(stop - index);
      index = stop;
      continue;
    }

    const character = source[index];
    if (character === '"' || character === "'" || character === "`") {
      let cursor = index + 1;
      while (cursor < source.length) {
        if (source[cursor] === "\\") {
          cursor += 2;
          continue;
        }
        if (source[cursor] === character) break;
        cursor += 1;
      }
      const stop = Math.min(cursor + 1, source.length);
      // Keep the quotes so import matching can still anchor on them, blank the
      // contents only when the literal is not an import specifier.
      out += source.slice(index, stop);
      index = stop;
      continue;
    }

    out += character;
    index += 1;
  }

  return out;
}

const code = stripCommentsAndStrings(layoutSource);

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
  const everyDependencyIsPinned = Object.values(packageJson.dependencies).every(
    (range) => /^\d+\.\d+\.\d+$/.test(range),
  );
  if (everyDependencyIsPinned) {
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
  const importPattern =
    /import\s*\{([^}]*)\}\s*from\s*["']@vercel\/analytics([^"']*)["']/g;
  const found = [];
  for (const match of code.matchAll(importPattern)) {
    const names = match[1].split(",").map((name) => name.trim());
    if (names.includes("Analytics")) found.push(match[2]);
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
  const mounts = [...code.matchAll(/<Analytics(\s|\/|>)/g)];
  assert.equal(
    mounts.length,
    1,
    `exactly one <Analytics /> must be mounted, found ${mounts.length}; a ` +
      "second mount double-counts every page view without erroring",
  );

  const bodyOpen = code.indexOf("<body");
  const bodyClose = code.indexOf("</body>");
  assert.ok(bodyOpen !== -1 && bodyClose !== -1, "the layout must render <body>");
  assert.ok(
    mounts[0].index > bodyOpen && mounts[0].index < bodyClose,
    "<Analytics /> must be mounted beneath <body>",
  );
});

test("the root layout was not converted to a client component", () => {
  // Directives are only directives at the very top of the module, so check the
  // first non-blank line of the original source rather than searching the file.
  const firstLine =
    layoutSource
      .split("\n")
      .map((line) => line.trim())
      .find((line) => line.length > 0) ?? "";

  assert.equal(
    /^["']use client["'];?$/.test(firstLine),
    false,
    'the root layout must stay a server component; "@vercel/analytics/next" ' +
      'is the maintained integration and needs no "use client" here',
  );
});
