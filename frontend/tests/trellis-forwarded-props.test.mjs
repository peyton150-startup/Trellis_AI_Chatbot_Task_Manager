import assert from "node:assert/strict";
import test from "node:test";

const transport = await import("../lib/TrellisHttpAgent.ts");

const { trellisForwardedProps, CONTINUITY_KEY, PREVIOUS_RUN_KEY } = transport;

test("the two reserved keys are the ones the backend extracts", () => {
  assert.equal(CONTINUITY_KEY, "trellisContinuityRunId");
  assert.equal(PREVIOUS_RUN_KEY, "trellisPreviousRunId");
});

test("unrelated forwarded properties survive untouched", () => {
  const forwarded = trellisForwardedProps(
    { somethingAssistantUiSent: { nested: true }, other: 1 },
    null,
    null,
  );

  assert.deepEqual(forwarded, {
    somethingAssistantUiSent: { nested: true },
    other: 1,
  });
});

test("each cursor is injected when present", () => {
  const forwarded = trellisForwardedProps({}, "continuity-id", "previous-id");

  assert.equal(forwarded[CONTINUITY_KEY], "continuity-id");
  assert.equal(forwarded[PREVIOUS_RUN_KEY], "previous-id");
});

test("each cursor is removed when absent, so a stale value cannot survive", () => {
  const forwarded = trellisForwardedProps(
    { [CONTINUITY_KEY]: "stale-continuity", [PREVIOUS_RUN_KEY]: "stale-previous" },
    null,
    null,
  );

  assert.ok(!(CONTINUITY_KEY in forwarded));
  assert.ok(!(PREVIOUS_RUN_KEY in forwarded));
});

test("the two cursors are independent and may legitimately differ", () => {
  // The D-76 case: a run committed a mutation and then failed, so continuity
  // stayed on the older completed run while the previous-run cursor advanced.
  // Undo targets the second value; conversation history comes from the first.
  const forwarded = trellisForwardedProps({}, "completed-run", "failed-run");

  assert.equal(forwarded[CONTINUITY_KEY], "completed-run");
  assert.equal(forwarded[PREVIOUS_RUN_KEY], "failed-run");
  assert.notEqual(forwarded[CONTINUITY_KEY], forwarded[PREVIOUS_RUN_KEY]);
});

test("one cursor set and the other clear is handled per key", () => {
  const onlyPrevious = trellisForwardedProps(
    { [CONTINUITY_KEY]: "stale" },
    null,
    "previous-id",
  );

  assert.ok(!(CONTINUITY_KEY in onlyPrevious));
  assert.equal(onlyPrevious[PREVIOUS_RUN_KEY], "previous-id");

  const onlyContinuity = trellisForwardedProps(
    { [PREVIOUS_RUN_KEY]: "stale" },
    "continuity-id",
    null,
  );

  assert.equal(onlyContinuity[CONTINUITY_KEY], "continuity-id");
  assert.ok(!(PREVIOUS_RUN_KEY in onlyContinuity));
});

test("a client-supplied value under either reserved key never passes through", () => {
  // Both keys are Trellis-owned. The backend treats them as untrusted lookup
  // keys either way, but a transport that forwarded a caller's guess would be
  // misreporting where the value came from.
  const overwritten = trellisForwardedProps(
    { [CONTINUITY_KEY]: "forged", [PREVIOUS_RUN_KEY]: "forged" },
    "real-continuity",
    "real-previous",
  );

  assert.equal(overwritten[CONTINUITY_KEY], "real-continuity");
  assert.equal(overwritten[PREVIOUS_RUN_KEY], "real-previous");
});

test("the caller's object is not mutated", () => {
  const incoming = { keep: "me", [CONTINUITY_KEY]: "stale" };
  const before = { ...incoming };

  trellisForwardedProps(incoming, "new-continuity", "new-previous");

  assert.deepEqual(incoming, before);
});

test("undefined incoming properties are accepted", () => {
  assert.deepEqual(trellisForwardedProps(undefined, null, null), {});
  assert.deepEqual(trellisForwardedProps(undefined, "c", "p"), {
    [CONTINUITY_KEY]: "c",
    [PREVIOUS_RUN_KEY]: "p",
  });
});
