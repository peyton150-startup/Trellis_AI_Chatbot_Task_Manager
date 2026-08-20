import { HttpAgent, type RunAgentInput } from "@ag-ui/client";

export const CONTINUITY_KEY = "trellisContinuityRunId";
export const PREVIOUS_RUN_KEY = "trellisPreviousRunId";

/**
 * The Trellis-owned forwarded properties, and only those.
 *
 * Extracted from `run()` so the assembly rule is testable without standing up
 * an HTTP transport. It is a pure function: it reads the caller's properties and
 * the two cursors, and returns a new object.
 *
 * Two rules, and both are load bearing.
 *
 * Unrelated properties survive. assistant-ui is free to forward its own, and
 * Trellis has no business dropping them.
 *
 * The two reserved keys are Trellis-owned outright. A null cursor deletes the
 * key rather than leaving whatever was there, so a stale value can never
 * outlive the state it came from, and a client-supplied value under either
 * reserved name is overwritten or removed rather than passed through. The
 * backend treats both as untrusted lookup keys regardless, but a transport that
 * forwarded a caller's guess at a run id would be lying about where the value
 * came from.
 */
export function trellisForwardedProps(
  incoming: Record<string, unknown> | undefined,
  continuityRunId: string | null,
  previousRunId: string | null,
): Record<string, unknown> {
  const forwardedProps: Record<string, unknown> = { ...(incoming ?? {}) };

  for (const [key, value] of [
    [CONTINUITY_KEY, continuityRunId],
    [PREVIOUS_RUN_KEY, previousRunId],
  ] as const) {
    if (value === null) {
      delete forwardedProps[key];
    } else {
      forwardedProps[key] = value;
    }
  }

  return forwardedProps;
}

/**
 * D-67 and D-76 browser transport.
 *
 * assistant-ui remains free to build its normal AG-UI payload. Immediately
 * before HttpAgent sends that payload, Trellis adds up to two optional
 * server-run lookup keys.
 *
 * They are two cursors because they answer two questions, and the D-76 defect
 * is what happens when one value tries to answer both:
 *
 *   continuityRunId  the newest run the server confirmed COMPLETED. It supplies
 *                    canonical conversation history for the next turn, and it
 *                    deliberately does not advance past a failed or interrupted
 *                    run, because such a run has no completed history to inherit.
 *
 *   previousRunId    the newest server-issued application run, whatever became
 *                    of it. It is the only candidate target of "undo that".
 *
 * A run that commits a task mutation and then fails leaves continuity behind on
 * an older run while remaining perfectly undoable. Undo has to target the run
 * the user just watched happen, so it reads the second cursor and never the
 * first.
 *
 * Neither value is history and neither is authorization. The backend resolves
 * both against actor-owned server state and discards forwardedProps before the
 * model sees the accepted RunAgentInput.
 */
export class TrellisHttpAgent extends HttpAgent {
  private continuityRunId: string | null = null;
  private previousRunId: string | null = null;

  setContinuityRunId(runId: string | null): void {
    this.continuityRunId = runId;
  }

  getContinuityRunId(): string | null {
    return this.continuityRunId;
  }

  setPreviousRunId(runId: string | null): void {
    this.previousRunId = runId;
  }

  getPreviousRunId(): string | null {
    return this.previousRunId;
  }

  override run(input: RunAgentInput): ReturnType<HttpAgent["run"]> {
    return super.run({
      ...input,
      forwardedProps: trellisForwardedProps(
        input.forwardedProps as Record<string, unknown> | undefined,
        this.continuityRunId,
        this.previousRunId,
      ),
    });
  }
}
