"use client";

import { useMemo } from "react";
import { HttpAgent } from "@ag-ui/client";
import { AssistantRuntimeProvider, useAuiEvent } from "@assistant-ui/react";
import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";
import { Thread } from "@/components/thread";
import { ApprovalCard } from "@/components/ApprovalCard";
import { NGROK_BYPASS_HEADERS } from "@/lib/ngrokDemoIngress";
import { useRun } from "@/lib/useRun";

interface ChatProps {
  onRunComplete: () => void | Promise<void>;
}

function RunCompletionListener({ onRunComplete }: ChatProps) {
  useAuiEvent("thread.runEnd", () => {
    void onRunComplete();
  });
  return null;
}

/**
 * T16. The approval surface, mounted inside the runtime provider.
 *
 * Separate from `Chat` because `useRun` reads the runtime's interrupts, and
 * separate from `Thread` because the card is not a tool-call rendering. It sits
 * directly under the transcript and above the composer, so a pending approval is
 * the last thing in the conversation and needs no scrolling, no expanding, and
 * no clicking to be understood.
 *
 * It renders nothing at all when the server reports no pending approval. The
 * card's presence is a statement that an approval exists, and only the server
 * gets to make that statement.
 */
function ApprovalSurface({ agent }: { agent: HttpAgent }) {
  const { card, deciding, pendingDecision, error, decide } = useRun(agent);

  if (card === null) return null;

  return (
    <div className="border-t p-3">
      <ApprovalCard
        card={card}
        deciding={deciding}
        pendingDecision={pendingDecision}
        error={error}
        onDecide={(decision) => void decide(decision)}
      />
    </div>
  );
}

export function Chat({ onRunComplete }: ChatProps) {
  const agent = useMemo(
    () =>
      new HttpAgent({
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
      <RunCompletionListener onRunComplete={onRunComplete} />
      <section
        aria-label="Assistant chat"
        className="mb-6 flex h-[42rem] min-h-[32rem] flex-col overflow-hidden border bg-background"
      >
        <div className="min-h-0 flex-1 overflow-hidden">
          <Thread />
        </div>
        <ApprovalSurface agent={agent} />
      </section>
    </AssistantRuntimeProvider>
  );
}
