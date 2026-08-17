import { HttpAgent, type RunAgentInput } from "@ag-ui/client";

const CONTINUITY_KEY = "trellisContinuityRunId";

/**
 * D-67 browser transport.
 *
 * assistant-ui remains free to build its normal AG-UI payload. Immediately
 * before HttpAgent sends that payload, Trellis adds exactly one optional
 * server-run lookup key.
 *
 * The value is not history and is not authorization. The backend resolves it
 * against actor-owned server state and discards forwardedProps before the model
 * sees the accepted RunAgentInput.
 */
export class TrellisHttpAgent extends HttpAgent {
  private continuityRunId: string | null = null;

  setContinuityRunId(runId: string | null): void {
    this.continuityRunId = runId;
  }

  getContinuityRunId(): string | null {
    return this.continuityRunId;
  }

  override run(input: RunAgentInput): ReturnType<HttpAgent["run"]> {
    const forwardedProps = {
      ...(input.forwardedProps ?? {}),
    };

    if (this.continuityRunId === null) {
      delete forwardedProps[CONTINUITY_KEY];
    } else {
      forwardedProps[CONTINUITY_KEY] = this.continuityRunId;
    }

    return super.run({
      ...input,
      forwardedProps,
    });
  }
}
