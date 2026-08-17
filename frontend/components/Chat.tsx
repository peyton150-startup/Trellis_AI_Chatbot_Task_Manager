"use client";

import { useEffect, useMemo, useRef } from "react";
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

  // D-67 keeps this separate from useRun(): T16 owns approval state, while
  // continuity only needs the server-issued application run id.
  useEffect(() => {
    const subscription = agent.subscribe({
      onRunStartedEvent: ({ event }) => {
        if (event.threadId) {
          currentRunId.current = event.threadId;
        }
      },
    });

    return () => subscription.unsubscribe();
  }, [agent]);

  useAuiEvent("thread.runEnd", () => {
    void onRunComplete();

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
