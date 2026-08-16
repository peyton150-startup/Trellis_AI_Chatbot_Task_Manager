"use client";

import { useMemo } from "react";
import { HttpAgent } from "@ag-ui/client";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiEvent,
} from "@assistant-ui/react";
import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";

interface ChatProps {
  onRunComplete: () => void | Promise<void>;
}

function RunCompletionListener({ onRunComplete }: ChatProps) {
  useAuiEvent("thread.runEnd", () => {
    void onRunComplete();
  });
  return null;
}

function ChatMessage() {
  return (
    <MessagePrimitive.Root>
      <MessagePrimitive.Content />
    </MessagePrimitive.Root>
  );
}

const MESSAGE_COMPONENTS = { Message: ChatMessage };

export function Chat({ onRunComplete }: ChatProps) {
  const agent = useMemo(() => new HttpAgent({ url: "/api/agui" }), []);
  const runtime = useAgUiRuntime({ agent });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RunCompletionListener onRunComplete={onRunComplete} />
      <section aria-label="Assistant chat">
        <ThreadPrimitive.Root>
          <ThreadPrimitive.Viewport>
            <ThreadPrimitive.Messages components={MESSAGE_COMPONENTS} />
            <ComposerPrimitive.Root>
              <ComposerPrimitive.Input
                aria-label="Message Trellis"
                placeholder="Ask Trellis to manage your tasks..."
              />
              <ComposerPrimitive.Send>Send</ComposerPrimitive.Send>
            </ComposerPrimitive.Root>
          </ThreadPrimitive.Viewport>
        </ThreadPrimitive.Root>
      </section>
    </AssistantRuntimeProvider>
  );
}
