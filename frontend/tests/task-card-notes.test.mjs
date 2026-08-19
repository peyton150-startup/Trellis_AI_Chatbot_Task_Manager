import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

/**
 * Source-contract regression for the notes line-break fix.
 *
 * This deliberately reads source text rather than rendering the component. The
 * property at stake is a CSS declaration and the absence of an HTML injection
 * path, and neither is observable from a render assertion without adding a
 * component-test stack the project does not have and does not otherwise need.
 * A whole framework for one declaration would cost more than it protects.
 *
 * What it does protect is real: `white-space: pre-wrap` is exactly the kind of
 * line a later tidy-up deletes as redundant, and the visible symptom is only
 * that multi-line notes silently render as one line again.
 */

const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
const taskCard = await readFile(
  new URL("../components/TaskCard.tsx", import.meta.url),
  "utf8",
);

function ruleBody(source, selector) {
  const start = source.indexOf(`\n${selector} {`);
  assert.notEqual(start, -1, `${selector} must exist in globals.css`);
  const end = source.indexOf("}", start);
  assert.notEqual(end, -1, `${selector} must be a closed rule`);
  return source.slice(start, end);
}

test("stored newlines survive into the rendered card", () => {
  const notes = ruleBody(css, ".task-card__notes");

  assert.match(
    notes,
    /white-space:\s*pre-wrap/,
    "notes carry real newline characters; without pre-wrap the card collapses them",
  );
});

test("preserved whitespace cannot let a long token overflow the card", () => {
  const notes = ruleBody(css, ".task-card__notes");

  assert.match(
    notes,
    /overflow-wrap:\s*anywhere/,
    "pre-wrap without anywhere lets one unbroken token push past the card edge",
  );
});

test("notes reach the DOM as escaped React text, never as markup", () => {
  assert.match(
    taskCard,
    /\{task\.notes \|\| "No notes"\}/,
    "notes must stay a React text child so React escapes them",
  );

  assert.doesNotMatch(
    taskCard,
    /dangerouslySetInnerHTML/,
    "notes are untrusted task data and must never be injected as HTML",
  );
});

test("line breaks are a rendering concern, not a string rewrite", () => {
  assert.doesNotMatch(
    taskCard,
    /<br\s*\/?>/i,
    "TaskCard must not hand-convert newlines into markup",
  );

  assert.doesNotMatch(
    taskCard,
    /notes[^\n]*\.split\(/,
    "notes must not be split into an array for rendering",
  );
});
