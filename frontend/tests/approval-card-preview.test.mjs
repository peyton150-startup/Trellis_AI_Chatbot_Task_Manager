import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { after, test } from "node:test";

import tsNamespace from "@typescript/typescript6";

const ts = tsNamespace.default ?? tsNamespace;

const source = await readFile(
  new URL("../lib/approvalPreview.ts", import.meta.url),
  "utf8",
);

const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;

const temporaryDirectory = await mkdtemp(
  join(tmpdir(), "trellis-approval-preview-"),
);
const compiledPath = join(temporaryDirectory, "approvalPreview.mjs");

await writeFile(compiledPath, compiled, "utf8");

const { affectedTasks } = await import(pathToFileURL(compiledPath).href);

after(async () => {
  await rm(temporaryDirectory, { recursive: true, force: true });
});

const task = (id) => ({
  id,
  title: `Task ${id}`,
});

test("bulk approval reports all 13 update targets when deletes is empty", () => {
  const updates = Array.from({ length: 13 }, (_, index) => task(index + 1));

  const result = affectedTasks({
    tool_name: "bulk_update_tasks",
    preview: {
      deletes: [],
      updates,
    },
  });

  assert.equal(result.length, 13);
  assert.deepEqual(result, updates);
});

test("bulk approval selects updates even if deletes is misleadingly nonempty", () => {
  const updates = [task(1), task(2), task(3)];

  const result = affectedTasks({
    tool_name: "bulk_update_tasks",
    preview: {
      deletes: [task(99)],
      updates,
    },
  });

  assert.deepEqual(result, updates);
});

test("delete approval selects deletes rather than updates", () => {
  const deletes = [task(7)];

  const result = affectedTasks({
    tool_name: "delete_tasks",
    preview: {
      deletes,
      updates: [task(50), task(51)],
    },
  });

  assert.deepEqual(result, deletes);
});

test("an unknown approval tool fails closed instead of guessing a list", () => {
  const result = affectedTasks({
    tool_name: "unknown_tool",
    preview: {
      deletes: [task(1)],
      updates: [task(2)],
    },
  });

  assert.deepEqual(result, []);
});
