/**
 * D-76 continuity reconciliation, extracted after the second neutral review.
 *
 * The first implementation kept this logic inline in `Chat.tsx` and resolved
 * "which run do I reconcile" from a single mutable `currentRunId` ref. A blind
 * review found the resulting race: `onRunFailed` for run B can arrive after
 * `RUN_STARTED` for run C has already overwritten that ref, so B's failure
 * reconciles C. Because promotion is gated on a server-confirmed `completed`
 * status, a stale ref can never promote the wrong run. It can, however, cause a
 * missed promotion: B completes durably on the server, B's transport drops,
 * the reconciler checks C instead, and B's canonical history is never adopted.
 * That is precisely the "server is right, browser is stale" case this mechanism
 * exists to close.
 *
 * The fix is a per-invocation binding rather than a shared cursor. Two distinct
 * identifiers are involved and conflating them is what caused the bug:
 *
 *     input.runId          the AG-UI invocation identifier. In Trellis's
 *                          current assistant-ui path the AG-UI client
 *                          generates it per invocation unless a caller
 *                          explicitly supplies one, which the protocol
 *                          permits. Used only as a local correlation key,
 *                          never as application authority, and never passed
 *                          to fetchRun(). Rebinding is therefore defined
 *                          behaviour rather than an assumed impossibility.
 *
 *     event.threadId       server-issued Trellis application run id, emitted
 *                          back on RUN_STARTED and RUN_FINISHED. The only
 *                          value that may be looked up or promoted.
 *
 * Note that `input.threadId` is NOT usable here. The AG-UI client sets it from
 * its own constant thread id, so it is identical across every run in a session
 * and cannot discriminate one invocation from another. It is also client
 * supplied, which the Trellis trust boundary treats as a lookup key at most.
 *
 * This module is deliberately free of React and of the transport, so the
 * reconciliation rule can be tested behaviorally instead of asserted with a
 * regular expression over component source.
 */

/** The subset of `RunDetail` this rule depends on. */
export interface ReconcilableRun {
  status: string;
}

export interface ReconcileContinuityOptions {
  /** A server-issued application run id. Never a client-supplied id. */
  runId: string | null;
  /** Asks the server for authoritative run state. */
  fetchRun: (runId: string) => Promise<ReconcilableRun>;
  /** Adopts `runId` as the continuity cursor. Called at most once. */
  promote: (runId: string) => void;
}

/**
 * Correlates one client invocation with the application run the server issued
 * for it, so a late lifecycle callback reconciles the run it belongs to rather
 * than whichever run happens to be newest.
 */
export class RunBindings {
  private readonly bindings = new Map<string, string>();

  /**
   * Records the application run the server issued for this invocation.
   *
   * Rebinding an existing invocation id overwrites, which keeps approval
   * continuation safe: a fresh invocation may legitimately resolve to an
   * application run that an earlier invocation already used, because one
   * `agent_runs.id` is one application run across approval continuation.
   */
  bind(invocationId: string, applicationRunId: string): void {
    this.bindings.set(invocationId, applicationRunId);
  }

  /**
   * Returns the application run for this invocation, or null when the
   * invocation was never bound. A null result must never be replaced by a
   * guess: an unbound failure means RUN_STARTED never arrived, so there is no
   * server-issued run to reconcile.
   */
  resolve(invocationId: string): string | null {
    return this.bindings.get(invocationId) ?? null;
  }

  /** Drops a finished invocation so the map does not grow without bound. */
  release(invocationId: string): void {
    this.bindings.delete(invocationId);
  }

  /** Live binding count. Exposed so tests can prove releases actually happen. */
  get size(): number {
    return this.bindings.size;
  }
}

/**
 * Asks the server about one specific application run and promotes it as the
 * continuity cursor only if PostgreSQL reports it `completed`.
 *
 * A transport failure is a reason to go and check, never a reason to assume.
 * `awaiting_approval`, `failed`, `interrupted`, and still-`running` runs leave
 * the previous authoritative cursor untouched, as does a failed status lookup.
 *
 * Resolves to true only when a promotion happened, which lets a test assert the
 * decision rather than infer it.
 */
export async function reconcileContinuity({
  runId,
  fetchRun,
  promote,
}: ReconcileContinuityOptions): Promise<boolean> {
  if (runId === null || runId === "") return false;

  try {
    const detail = await fetchRun(runId);
    if (detail.status === "completed") {
      promote(runId);
      return true;
    }
    return false;
  } catch {
    // A failed status lookup must never guess that the run completed.
    // Keep the previous authoritative continuity cursor.
    return false;
  }
}
