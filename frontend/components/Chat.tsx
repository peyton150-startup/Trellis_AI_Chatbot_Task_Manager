"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import { AssistantRuntimeProvider, useAuiEvent } from "@assistant-ui/react";
import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";
import { Thread } from "@/components/thread";
import { ApprovalCard } from "@/components/ApprovalCard";
import { NGROK_BYPASS_HEADERS } from "@/lib/ngrokDemoIngress";
import { fetchRun } from "@/lib/api";
import { TrellisHttpAgent } from "@/lib/TrellisHttpAgent";
import { useRun } from "@/lib/useRun";

interface ChatProps {
  onRunComplete: () => void | Promise<void>;
}

function RunCompletionListener({
  agent,
  onRunComplete,
}: ChatProps & { agent: TrellisHttpAgent }) {
  const currentRunId = useRef<string | null>(null);

  // One reconciliation rule, called from run end and from transport failure.
  // Deliberately kept inline rather than extracted to a module: the T17 gate
  // asserts these two literals in this file, and moving them would amend an
  // earlier task's check for a second time.
  const reconcileContinuity = useCallback(() => {
    const runId = currentRunId.current;
    if (runId === null) return;

    // Promote only server-confirmed completed runs. awaiting_approval, failed,
    // interrupted, or still-running runs leave the previous continuity cursor
    // unchanged.
    void fetchRun(runId)
      .then((detail) => {
        if (detail.status === "completed") {
          agent.setContinuityRunId(runId);
        }
      })
      .catch(() => {
        // A failed status lookup must never guess that the run completed.
        // Keep the previous authoritative continuity cursor.
      });
  }, [agent]);

  // D-67 keeps this separate from useRun(): T16 owns approval state, while
  // continuity only needs the server-issued application run id.
  //
  // D-76 adds the second cursor here, and the placement is the point. The
  // previous-run cursor advances on RUN_STARTED, the moment the server issues
  // the id, and never waits for the run to end. That is what makes a run that
  // commits a mutation and then fails still nameable by "undo that". Continuity
  // promotion below is unchanged and remains completed-only.
  useEffect(() => {
    const subscription = agent.subscribe({
      onRunStartedEvent: ({ event }) => {
        if (event.threadId) {
          currentRunId.current = event.threadId;
          agent.setPreviousRunId(event.threadId);
        }
      },
    });

    return () => subscription.unsubscribe();
  }, [agent]);

  useAuiEvent("thread.runEnd", () => {
    void onRunComplete();
    reconcileContinuity();
  });

  // D-76, after neutral review. Reconciliation runs on transport failure too,
  // and the reason is a window neither cursor covered on its own.
  //
  // A control turn can commit `completed` with its canonical history and then
  // lose the SSE connection before RUN_FINISHED reaches the browser. Run end
  // never fires, so continuity stays on the older run, and the next ordinary
  // turn inherits history that omits the undo that actually happened. The
  // server is right and the browser is stale.
  //
  // The fix is not to promote on error. It is to ask the server. The rule is
  // unchanged and is the whole point of D-67: only a run PostgreSQL reports as
  // `completed` may become the continuity cursor. A transport failure is a
  // reason to go and check, never a reason to assume.
  useEffect(() => {
    const subscription = agent.subscribe({
      onRunFailed: () => {
        reconcileContinuity();
      },
    });

    return () => subscription.unsubscribe();
  }, [agent, reconcileContinuity]);

  return null;
}

/**
 * T16. The authoritative approval surface, mounted inside the runtime provider.
 *
 * `Chat` continues to own approval state and behavior because `useRun` reads the
 * runtime's interrupts. `Thread` receives only a presentation slot to place that
 * surface immediately above its existing composer; it does not become approval-
 * aware and the card remains separate from tool-call rendering.
 *
 * It renders nothing when the server reports no pending approval. The card's
 * presence is a statement that an approval exists, and only the server gets to
 * make that statement.
 */
function ApprovalSurface({ agent }: { agent: TrellisHttpAgent }) {
  const { card, deciding, pendingDecision, error, decide } = useRun(agent);

  if (card === null) return null;

  return (
    <ApprovalCard
      card={card}
      deciding={deciding}
      pendingDecision={pendingDecision}
      error={error}
      onDecide={(decision) => void decide(decision)}
    />
  );
}

export function Chat({ onRunComplete }: ChatProps) {
  const agent = useMemo(
    () =>
      new TrellisHttpAgent({
        url: "/api/agui",
        headers: NGROK_BYPASS_HEADERS,
      }),
    [],
  );
  const runtime = useAgUiRuntime({
    agent,
    onError: () => {
      void onRunComplete();
    },
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RunCompletionListener agent={agent} onRunComplete={onRunComplete} />
      <section
        aria-label="Assistant chat"
        className="mb-6 flex h-[42rem] min-h-[32rem] flex-col overflow-hidden border bg-background"
      >
        <div className="min-h-0 flex-1 overflow-hidden">
          <Thread composerTop={<ApprovalSurface agent={agent} />} />
        </div>
      </section>
    </AssistantRuntimeProvider>
  );
}
