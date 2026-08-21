import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";

const tools = await import("../lib/agentTools.ts");
const { AGENT_TOOL_LABELS } = tools;

// Line endings are normalized because the repository checks out CRLF on
// Windows, and every pattern below is written against LF.
const repoFile = (relative) =>
  readFileSync(
    fileURLToPath(new URL(`../../${relative}`, import.meta.url)),
    "utf8",
  ).replace(/\r\n/g, "\n");

/**
 * The backend enum is the authority for which tools exist. Parsing it here is
 * the whole point of this file: a frontend-only assertion that eight hardcoded
 * labels equal eight hardcoded labels stays green forever, including on the day
 * a ninth tool lands in the backend and the page header starts lying.
 */
function backendToolNames() {
  const source = repoFile("backend/app/models.py");
  const body = source.match(/class ToolName\(str, Enum\):\n((?:\s{4}.*\n|\n)*)/);
  assert.ok(body, "could not find the ToolName enum in backend/app/models.py");

  const names = [...body[1].matchAll(/^\s{4}[A-Z_]+ = "([a-z_]+)"$/gm)].map(
    (match) => match[1],
  );
  assert.ok(names.length > 0, "parsed no members out of the ToolName enum");
  return names;
}

function titleCase(toolName) {
  return toolName
    .split("_")
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(" ");
}

test("every backend tool is shown, and nothing else is", () => {
  const expected = backendToolNames().map(titleCase);

  // Compared as sets, because display order is a presentation choice and is
  // deliberately not the enum's declaration order.
  assert.deepEqual(
    [...AGENT_TOOL_LABELS].sort(),
    [...expected].sort(),
    "the header tool list and the backend ToolName enum disagree",
  );
});

test("the browser profile is the whole enum, not the Linear subset", () => {
  // ALL_TOOLS is every ToolName; LINEAR_TOOLS is a smaller frozenset and must
  // never drive this display. If the browser profile stops being ALL_TOOLS,
  // this list needs rethinking rather than quietly staying correct by accident.
  const agent = repoFile("backend/app/agent.py");

  assert.match(
    agent,
    /ALL_TOOLS = frozenset\(name\.value for name in ToolName\)/,
    "the browser profile is no longer every ToolName",
  );
  assert.equal(AGENT_TOOL_LABELS.length, backendToolNames().length);
});

test("the labels are human readable, not wire names", () => {
  for (const label of AGENT_TOOL_LABELS) {
    assert.ok(!label.includes("_"), `${label} is a snake_case wire name`);
    assert.match(label, /^[A-Z]/, `${label} is not Title Case`);
  }
});

test("the display order is the one chosen for a demo, and is asserted", () => {
  assert.deepEqual(
    [...AGENT_TOOL_LABELS],
    [
      "Create Task",
      "Update Task",
      "List Tasks",
      "Bulk Update Tasks",
      "Delete Tasks",
      "Get Task History",
      "Resolve Task Reference",
      "Propose Plan",
    ],
  );
});

test("the page renders every label and keeps the authoritative-state note", () => {
  const page = repoFile("frontend/app/page.tsx");

  assert.match(
    page,
    /AGENT_TOOL_LABELS/,
    "the header does not render the shared tool list",
  );
  assert.match(
    page,
    /reads task state from FastAPI/,
    "the authoritative-state explanation was dropped",
  );

  // The list must come from the shared module rather than being retyped into
  // the page, or the cross-boundary check above stops protecting the header.
  for (const label of AGENT_TOOL_LABELS) {
    assert.ok(
      !page.includes(`"${label}"`),
      `${label} is hardcoded in page.tsx instead of coming from the module`,
    );
  }
});

test("the display list is not read from client-supplied AG-UI tools", () => {
  // Trellis rebuilds incoming AG-UI input with client tools empty on purpose.
  // Sourcing the header from that array would let a client claim about
  // capabilities become the thing Trellis displays as fact.
  const module = repoFile("frontend/lib/agentTools.ts");
  const page = repoFile("frontend/app/page.tsx");

  for (const source of [module, page]) {
    assert.ok(
      !/\binput\.tools\b/.test(source) && !/forwardedProps/.test(source),
      "the tool display reads from client-supplied AG-UI input",
    );
  }
});
