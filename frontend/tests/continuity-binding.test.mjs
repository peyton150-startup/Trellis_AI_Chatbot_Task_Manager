import assert from "node:assert/strict";
import test from "node:test";

const continuity = await import("../lib/continuity.ts");

const { RunBindings, reconcileContinuity } = continuity;

/**
 * A recording stand-in for the real fetchRun. Records every run id it was
 * asked about, which is what lets the central regression below assert that the
 * WRONG run was never even queried, not merely that it was not promoted.
 */
function statusSource(statusByRunId) {
  const asked = [];
  return {
    asked,
    fetchRun: async (runId) => {
      asked.push(runId);
      const status = statusByRunId[runId];
      if (status === undefined) throw new Error(`no such run ${runId}`);
      return { status };
    },
  };
}

function promoter() {
  const promoted = [];
  return { promoted, promote: (runId) => promoted.push(runId) };
}

test("a delayed failure reconciles its own run, not the newest one", async () => {
  // The defect a blind review found in the shared-ref implementation.
  //
  //   RUN_STARTED B    client-B -> server-B
  //   server-B commits completed
  //   RUN_STARTED C    client-C -> server-C
  //   delayed onRunFailed(client-B)
  //
  // The old code read a single mutable cursor, which run C had already
  // overwritten, so B's failure asked about C. B's durable completion was then
  // never adopted and the next turn inherited stale history.
  const bindings = new RunBindings();
  const source = statusSource({ "server-B": "completed", "server-C": "running" });
  const target = promoter();

  bindings.bind("client-B", "server-B");
  bindings.bind("client-C", "server-C");

  const promoted = await reconcileContinuity({
    runId: bindings.resolve("client-B"),
    fetchRun: source.fetchRun,
    promote: target.promote,
  });

  assert.equal(promoted, true);
  assert.deepEqual(source.asked, ["server-B"]);
  assert.ok(!source.asked.includes("server-C"), "must not query the newer run");
  assert.deepEqual(target.promoted, ["server-B"]);
});

test("RUN_FINISHED reconciles the run the event itself names", async () => {
  const source = statusSource({ "server-B": "completed", "server-C": "completed" });
  const target = promoter();

  await reconcileContinuity({
    runId: "server-C",
    fetchRun: source.fetchRun,
    promote: target.promote,
  });

  assert.deepEqual(source.asked, ["server-C"]);
  assert.deepEqual(target.promoted, ["server-C"]);
});

test("only a server-confirmed completed run is promoted", async () => {
  for (const status of ["failed", "running", "awaiting_approval", "interrupted"]) {
    const source = statusSource({ "server-B": status });
    const target = promoter();

    const promoted = await reconcileContinuity({
      runId: "server-B",
      fetchRun: source.fetchRun,
      promote: target.promote,
    });

    assert.equal(promoted, false, `${status} must not promote`);
    assert.deepEqual(source.asked, ["server-B"], `${status} must still ask`);
    assert.deepEqual(target.promoted, [], `${status} must not promote`);
  }
});

test("an unbound failed invocation promotes nothing and guesses no id", async () => {
  // onRunFailed can arrive without RUN_STARTED ever having landed, for example
  // when the connection is refused outright. There is no server-issued run to
  // ask about, and inventing one would be worse than doing nothing.
  const bindings = new RunBindings();
  const source = statusSource({ "server-B": "completed" });
  const target = promoter();

  bindings.bind("client-B", "server-B");

  const promoted = await reconcileContinuity({
    runId: bindings.resolve("client-unknown"),
    fetchRun: source.fetchRun,
    promote: target.promote,
  });

  assert.equal(promoted, false);
  assert.deepEqual(source.asked, [], "an unbound invocation must query nothing");
  assert.deepEqual(target.promoted, []);
});

test("a failed status lookup never guesses that the run completed", async () => {
  const target = promoter();

  const promoted = await reconcileContinuity({
    runId: "server-B",
    fetchRun: async () => {
      throw new Error("network down");
    },
    promote: target.promote,
  });

  assert.equal(promoted, false);
  assert.deepEqual(target.promoted, []);
});

test("approval continuation may rebind an invocation to the same application run", async () => {
  // One agent_runs.id is one application run, and approval continuation creates
  // a new model invocation under that same application run. Rebinding must be
  // safe and must resolve to the authoritative run.
  const bindings = new RunBindings();

  bindings.bind("client-B", "server-B");
  bindings.bind("client-continuation", "server-B");

  assert.equal(bindings.resolve("client-B"), "server-B");
  assert.equal(bindings.resolve("client-continuation"), "server-B");

  bindings.bind("client-B", "server-B-again");
  assert.equal(bindings.resolve("client-B"), "server-B-again");
});

test("finished invocations are released so the map does not grow without bound", () => {
  const bindings = new RunBindings();

  bindings.bind("client-B", "server-B");
  bindings.bind("client-C", "server-C");
  assert.equal(bindings.size, 2);

  bindings.release("client-B");
  assert.equal(bindings.size, 1);
  assert.equal(bindings.resolve("client-B"), null);
  assert.equal(bindings.resolve("client-C"), "server-C");

  // Releasing an unknown invocation is a no-op rather than an error.
  bindings.release("client-unknown");
  assert.equal(bindings.size, 1);
});

test("a null or empty run id promotes nothing and queries nothing", async () => {
  for (const runId of [null, ""]) {
    const source = statusSource({});
    const target = promoter();

    const promoted = await reconcileContinuity({
      runId,
      fetchRun: source.fetchRun,
      promote: target.promote,
    });

    assert.equal(promoted, false);
    assert.deepEqual(source.asked, []);
    assert.deepEqual(target.promoted, []);
  }
});
