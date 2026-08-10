"use client";

import {
  ComposerPrimitive,
  MessagePartPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  type ToolCallMessagePartProps,
  useAuiState,
} from "@assistant-ui/react";
import { ApprovalCard } from "./approval-card";

function TextPart() {
  return <MessagePartPrimitive.Text className="message-text" component="p" />;
}

function ToolPart({
  toolName,
  args,
  result,
}: ToolCallMessagePartProps<Record<string, unknown>>) {
  return (
    <div className="tool-part" data-testid="tool-part">
      <span className="tool-part__label">Tool request</span>
      <code>{toolName}</code>
      <pre>{JSON.stringify(args, null, 2)}</pre>
      {result !== undefined ? (
        <p className="tool-part__result">Result: {JSON.stringify(result)}</p>
      ) : null}
    </div>
  );
}

function Message() {
  const role = useAuiState((state) => state.message.role);
  return (
    <MessagePrimitive.Root className={`message message--${role}`}>
      <span className="message-role">{role === "user" ? "You" : "Trellis"}</span>
      <MessagePrimitive.Parts
        components={{
          Text: TextPart,
          tools: { Fallback: ToolPart },
        }}
      />
    </MessagePrimitive.Root>
  );
}

export function ChatThread() {
  return (
    <ThreadPrimitive.Root className="thread-root">
      <ThreadPrimitive.Viewport className="thread-viewport">
        <ThreadPrimitive.Empty>
          <div className="empty-state">
            <span className="empty-state__mark">T</span>
            <h2>Start with a transport proof</h2>
            <p>
              Send any message to prove streaming, or ask to delete a task to
              exercise the interrupt path.
            </p>
          </div>
        </ThreadPrimitive.Empty>
        <ThreadPrimitive.Messages components={{ Message }} />
        <ApprovalCard />
        <ThreadPrimitive.ViewportFooter className="composer-dock">
          <ComposerPrimitive.Root className="composer">
            <ComposerPrimitive.Input
              aria-label="Message"
              autoFocus
              className="composer-input"
              placeholder="Try: Delete the demo task"
              rows={1}
            />
            <ComposerPrimitive.Send className="send-button" aria-label="Send message">
              <span>Send</span>
              <span aria-hidden="true">↗</span>
            </ComposerPrimitive.Send>
          </ComposerPrimitive.Root>
          <p className="composer-hint">Enter to send · Shift+Enter for a new line</p>
        </ThreadPrimitive.ViewportFooter>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
}
