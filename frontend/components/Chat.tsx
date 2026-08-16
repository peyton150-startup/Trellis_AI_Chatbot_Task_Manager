"use client";

import { useMemo } from "react";
import { HttpAgent } from "@ag-ui/client";
import { AssistantRuntimeProvider, useAuiEvent } from "@assistant-ui/react";
import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";
import { Thread } from "@/components/thread";
import { NGROK_BYPASS_HEADERS } from "@/lib/ngrokDemoIngress";

interface ChatProps {
  onRunComplete: () => void | Promise<void>;
}

function RunCompletionListener({ onRunComplete }: ChatProps) {
  useAuiEvent("thread.runEnd", () => {
    void onRunComplete();
  });
  return null;
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
  const runtime = useAgUiRuntime({ agent });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RunCompletionListener onRunComplete={onRunComplete} />
      <section
        aria-label="Assistant chat"
        className="mb-6 h-[42rem] min-h-[32rem] overflow-hidden border bg-background"
      >
        <Thread />
      </section>
    </AssistantRuntimeProvider>
  );
}
