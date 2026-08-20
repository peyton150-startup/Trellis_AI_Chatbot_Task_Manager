"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import { AssistantRuntimeProvider, useAuiEvent } from "@assistant-ui/react";
import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";
import { Thread } from "@/components/thread";
import { ApprovalCard } from "@/components/ApprovalCard";
import { NGROK_BYPASS_HEADERS } from "@/lib/ngrokDemoIngress";
import { fetchRun } from "@/lib/api";
import { RunBindings, reconcileContinuity } from "@/lib/continuity";
import { TrellisHttpAgent } from "@/lib/TrellisHttpAgent";
import { useRun } from "@/lib/useRun";

interface ChatProps {
  onRunComplete: () => void | Promise<void>;
}

function RunCompletionListener({
  agent,
  onRunComplete,
}: ChatProps & { agent: TrellisHttpAgent }) {
  // One binding per client invocation, so a late callback reconciles the run it
  // belongs to. See lib/continuity.ts for why a single mutable cursor raced.
  const runBindings = useRef(new RunBindings());

  // One reconciliation rule, now taking the run explicitly instead of reading
  // whichever run happens to be current at callback time.
  const reconcile = useCallback(
    (runId: string | null) =>
      reconcileContinuity({
        runId,
        fetchRun,
        promote: (completedRunId) => agent.setContinuityRunId(completedRunId),
      }),
    [agent],
  );

  // D-67 keeps this separate from useRun(): T16 owns approval state, while
  // continuity only needs the server-issued application run id.
  //
  // D-76 adds the second cursor here, and the placement is the point. The
  // previous-run cursor advances on RUN_STARTED, the moment the server issues
  // the id, and never waits for the run to end. That is what makes a run that
  // commits a mutation and then fails still nameable by "undo that".
  //
  // After the second neutral review, all three lifecycle callbacks are bound to
  // the same subscription and all three carry the invocation that produced
  // them. RUN_STARTED establishes the invocation to application-run binding,
  // RUN_FINISHED reconciles the exact run it names, and a transport failure
  // resolves its own invocation back to the run the server issued for it.
  // Continuity promotion is unchanged and remains completed-only.
  useEffect(() => {
    const bindings = runBindings.current;
    const subscription = agent.subscribe({
      onRunStartedEvent: ({ event, input }) => {
        if (event.threadId) {
          bindings.bind(input.runId, event.threadId);
          agent.setPreviousRunId(event.threadId);
        }
      },

      // Reconcile the run this event names, not the newest one. The completed
      // only guard means an interrupt outcome is safe to pass through: the
      // server reports awaiting_approval and nothing is promoted.
      onRunFinishedEvent: ({ event, input }) => {
        void reconcile(event.threadId);
        bindings.release(input.runId);
      },

      // D-76, after neutral review. Reconciliation runs on transport failure
      // too, and the reason is a window neither cursor covered on its own.
      //
      // A control turn can commit `completed` with its canonical history and
      // then lose the SSE connection before RUN_FINISHED reaches the browser.
      // Run end never fires, so continuity stays on the older run, and the next
      // ordinary turn inherits history that omits the undo that actually
      // happened. The server is right and the browser is stale.
      //
      // The fix is not to promote on error. It is to ask the server, about this
      // invocation's own run. An unbound invocation means RUN_STARTED never
      // arrived, so there is no server-issued run to ask about and nothing is
      // guessed.
      onRunFailed: ({ input }) => {
        void reconcile(bindings.resolve(input.runId));
        bindings.release(input.runId);
      },
    });

    return () => subscription.unsubscribe();
  }, [agent, reconcile]);

  // Board refresh only. Continuity identity comes from the AG-UI lifecycle
  // events above, which carry the server-issued run id; this one does not.
  useAuiEvent("thread.runEnd", () => {
    void onRunComplete();
  });

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
