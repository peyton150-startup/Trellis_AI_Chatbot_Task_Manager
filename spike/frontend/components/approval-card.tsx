"use client";

import {
  useAgUiInterrupts,
  useAgUiSubmitInterruptResponses,
} from "@assistant-ui/react-ag-ui";
import { useState } from "react";

export function ApprovalCard() {
  const interrupts = useAgUiInterrupts();
  const submitResponses = useAgUiSubmitInterruptResponses();
  const [decision, setDecision] = useState<"approve" | "deny" | null>(null);

  if (interrupts.length === 0) return null;

  const interrupt = interrupts[0];
  if (!interrupt) return null;

  const submit = async (approved: boolean) => {
    setDecision(approved ? "approve" : "deny");
    try {
      await submitResponses(
        interrupts.map((entry) => ({
          interruptId: entry.id,
          status: "resolved" as const,
          payload: { approved },
        })),
      );
    } finally {
      setDecision(null);
    }
  };

  return (
    <section className="approval-card" aria-label="Approval required">
      <div className="approval-card__eyebrow">
        <span className="pulse-dot" />
        Human decision required
      </div>
      <h3>Delete the demo task?</h3>
      <p>
        The agent requested a destructive tool. The tool body is blocked until
        this interrupt is resolved.
      </p>
      <dl className="approval-meta">
        <div>
          <dt>Interrupt</dt>
          <dd data-testid="interrupt-id">{interrupt.id}</dd>
        </div>
        <div>
          <dt>Tool call</dt>
          <dd data-testid="tool-call-id">{interrupt.toolCallId ?? "pending"}</dd>
        </div>
      </dl>
      <div className="approval-actions">
        <button
          className="button button--approve"
          disabled={decision !== null}
          onClick={() => void submit(true)}
          type="button"
        >
          {decision === "approve" ? "Approving..." : "Approve deletion"}
        </button>
        <button
          className="button button--deny"
          disabled={decision !== null}
          onClick={() => void submit(false)}
          type="button"
        >
          {decision === "deny" ? "Denying..." : "Deny"}
        </button>
      </div>
    </section>
  );
}
