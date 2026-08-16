"use client";

/**
 * T16. The browser half of the approval bridge.
 *
 * This module exists because answering the framework interrupt is not the same
 * thing as deciding an approval, and the shipped UI did only the first. The
 * approvals row is the authorization record under D-06, and until
 * `POST /api/runs/{id}/approvals/{tool_call_id}` has written a decision into it,
 * a continuation is refused outright: continuation eligibility requires a
 * decided row, so an undecided one matches nothing and the transport answers
 * 403. Approve looked identical to Reject because neither one ever ran.
 *
 * So the order below is the whole point of the file, and it is not
 * interchangeable:
 *
 *   1. POST the decision. The server verifies ownership, run state, existence,
 *      expiry, and pending status, then persists through a guarded update.
 *   2. Only if that succeeded, submit the AG-UI interrupt response, which is
 *      what sends `resume[]` and lets the server build `ToolApproved` from the
 *      row it just wrote.
 *
 * Reversing them would submit a continuation against an undecided row and
 * reproduce the original defect exactly.
 *
 * What this module is not allowed to do is settled by BUILD_SPEC section 12 and
 * D-06, and the code is arranged so a reader can check it. It reads the run id
 * off `RUN_STARTED`, reads the card off `GET /api/runs/{id}`, and sends one
 * enum value back. It derives no approval identity, no authorization, no
 * preview, no expiry, and no mutation arguments, and it never treats a rendered
 * card as evidence that an approval exists.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AbstractAgent } from "@ag-ui/client";
import {
  useAgUiInterrupts,
  useAgUiSubmitInterruptResponses,
} from "@assistant-ui/react-ag-ui";

import { fetchRun, postApprovalDecision } from "./api";
import type { Task } from "./types";

/** The interrupt id the server issues for a deferred call. Fixed at Gate A. */
const INTERRUPT_PREFIX = "int-";

export type ApprovalDecision = "approved" | "denied";

/**
 * The server's approval card, exactly as section 9 defines it.
 *
 * Deliberately narrower than the `approvals` row. There is no `arguments` and
 * no `arguments_hash` here, because the client renders a decision it has no
 * authority over and does not need the values the tool body will verify.
 */
export interface PendingApproval {
  tool_call_id: string;
  tool_name: string;
  required_reason: string;
  preview: { deletes?: Task[]; updates?: Task[] };
  expires_at: string;
}

export interface RunDetail {
  id: string;
  status: string;
  prompt: string;
  pending_approval: PendingApproval | null;
  error: string | null;
}

export interface RunState {
  /** The server-issued `agent_runs.id`, learned from `RUN_STARTED`. */
  runId: string | null;
  /** The authoritative card, or null when nothing needs a decision now. */
  card: PendingApproval | null;
  /** True from the first click until the continuation settles. */
  deciding: boolean;
  /** The decision in flight, so the card can say which button was pressed. */
  pendingDecision: ApprovalDecision | null;
  /** A server refusal, surfaced instead of a fabricated success. */
  error: string | null;
  decide: (decision: ApprovalDecision) => Promise<void>;
}

/**
 * Track one application run and drive its approval decision.
 *
 * Must be called inside `AssistantRuntimeProvider`, because the interrupt hooks
 * read the runtime's thread. `agent` is the same instance passed to
 * `useAgUiRuntime`, and it must be referentially stable across renders or the
 * subscription below churns.
 */
export function useRun(agent: AbstractAgent): RunState {
  const [runId, setRunId] = useState<string | null>(null);
  const [card, setCard] = useState<PendingApproval | null>(null);
  const [deciding, setDeciding] = useState(false);
  const [pendingDecision, setPendingDecision] =
    useState<ApprovalDecision | null>(null);
  const [error, setError] = useState<string | null>(null);

  const interrupts = useAgUiInterrupts();
  const submitInterruptResponses = useAgUiSubmitInterruptResponses();

  // A second click must not produce a second decision or a second continuation.
  // React state alone is not enough: two clicks inside one render commit both
  // read the same `deciding` value, so the guard has to be a ref that flips
  // synchronously in the handler.
  const inFlight = useRef(false);

  /**
   * The application run id, from `RUN_STARTED.threadId`.
   *
   * The server puts `agent_runs.id` in `thread_id` on the input it builds for
   * itself, and the adapter echoes it outward. This is how the browser learns
   * an id it is not permitted to mint, which is why the value is read from the
   * event rather than from the `threadId` this client sent, a field the server
   * reads for nothing.
   */
  useEffect(() => {
    const subscription = agent.subscribe({
      onRunStartedEvent: ({ event }) => {
        if (event.threadId) setRunId(event.threadId);
      },
    });
    return () => subscription.unsubscribe();
  }, [agent]);

  /**
   * Load the card whenever the framework reports a pending interrupt.
   *
   * The interrupt is the trigger, not the source. It tells the browser that
   * something is waiting; the server tells it what. A run whose interrupt is
   * pending but whose `pending_approval` is null is a state the card must
   * render as absent rather than paper over, since under D-57 that is exactly
   * what a run looks like once its decision is already persisted.
   */
  const hasInterrupt = interrupts.length > 0;
  useEffect(() => {
    if (!hasInterrupt || runId === null) {
      if (!hasInterrupt) setCard(null);
      return;
    }

    let cancelled = false;
    void fetchRun(runId)
      .then((detail) => {
        if (!cancelled) setCard(detail.pending_approval);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setCard(null);
          setError(
            cause instanceof Error
              ? cause.message
              : "Could not load the approval record.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [hasInterrupt, runId]);

  /**
   * The framework interrupt that corresponds to the server's card.
   *
   * Matched rather than derived. The card supplies the call id and the runtime
   * supplies the interrupt id, and this checks that the two describe the same
   * deferred call instead of constructing one identifier from the other. If
   * they do not correspond there is nothing safe to submit, so `decide` refuses
   * rather than guessing which interrupt the human answered.
   */
  const matchedInterrupt = useMemo(() => {
    if (card === null) return null;
    const expected = `${INTERRUPT_PREFIX}${card.tool_call_id}`;
    return (
      interrupts.find(
        (candidate) =>
          candidate.id === expected ||
          candidate.toolCallId === card.tool_call_id,
      ) ?? null
    );
  }, [card, interrupts]);

  const decide = useCallback(
    async (decision: ApprovalDecision) => {
      if (inFlight.current) return;
      if (runId === null || card === null) return;
      if (matchedInterrupt === null) {
        setError(
          "The pending approval does not match any interrupt on this thread.",
        );
        return;
      }

      inFlight.current = true;
      setDeciding(true);
      setPendingDecision(decision);
      setError(null);

      try {
        // Step 1. Authoritative, and the step the previous implementation
        // skipped. A refusal here throws, so the continuation below never runs
        // and the tool is never offered a decision the server did not record.
        await postApprovalDecision(runId, card.tool_call_id, decision);

        // Step 2. The framework continuation. `payload` is carried because the
        // resume entry shape declares it, and the server reads it for nothing:
        // `_deferred_results` builds `ToolApproved` or `ToolDenied` from the
        // persisted row alone. Sending it is honest about what the browser
        // believes without making that belief load bearing.
        await submitInterruptResponses([
          {
            interruptId: matchedInterrupt.id,
            status: "resolved",
            payload: { decision },
          },
        ]);

        // The card is cleared only now, after the continuation settled. The
        // board is refreshed by the existing `thread.runEnd` listener, from
        // committed state, so nothing here claims an outcome it has not seen.
        setCard(null);
      } catch (cause: unknown) {
        // A failure at either step must not read as success. The card stays on
        // screen carrying the message, because the user's question, "did my
        // deletion happen", is not answered by removing the thing that asked it.
        setError(
          cause instanceof Error
            ? cause.message
            : "The approval could not be completed.",
        );
      } finally {
        inFlight.current = false;
        setDeciding(false);
        setPendingDecision(null);
      }
    },
    [runId, card, matchedInterrupt, submitInterruptResponses],
  );

  return { runId, card, deciding, pendingDecision, error, decide };
}
