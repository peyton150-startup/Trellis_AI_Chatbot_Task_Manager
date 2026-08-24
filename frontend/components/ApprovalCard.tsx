"use client";

/**
 * T16. The approval card.
 *
 * Presentation only, and deliberately so. BUILD_SPEC section 12 puts it plainly:
 * if a decision about approval behaviour appears to live in this file, it is in
 * the wrong file. Everything here reads from the server's `pending_approval`
 * projection and calls back into `useRun`. It derives no identity, no
 * authorization, no preview, and no expiry, and it renders whatever the server
 * says is pending rather than what the tool call arguments implied.
 *
 * The one product rule this file does own is that the consequence is visible
 * before the decision. The card is not a collapsed tool call, a disclosure
 * triangle, a popover, or a modal that has to be opened. When an approval is
 * pending, the affected tasks are already on screen, because a user who has to
 * click something to discover what they are approving is being asked to approve
 * blind.
 */

import type { ApprovalDecision, PendingApproval } from "@/lib/useRun";
import { affectedTasks } from "@/lib/approvalPreview";
import { Button } from "@/components/ui/button";

interface ApprovalCardProps {
  card: PendingApproval;
  deciding: boolean;
  pendingDecision: ApprovalDecision | null;
  error: string | null;
  onDecide: (decision: ApprovalDecision) => void;
}

/**
 * The headline, per tool.
 *
 * A lookup rather than a formatter, so an unrecognised tool name produces a
 * deliberately vague heading and the preview below still carries the detail.
 * The alternative, interpolating the raw tool name into a sentence, would put
 * a server string into a claim about what is going to happen.
 */
const ACTION_HEADINGS: Record<string, string> = {
  delete_tasks: "Delete task",
  bulk_update_tasks: "Update tasks",
};

const CONSEQUENCES: Record<string, (count: number) => string> = {
  delete_tasks: (count) =>
    count === 1
      ? "This action will permanently delete this task."
      : `This action will permanently delete these ${count} tasks.`,
  bulk_update_tasks: (count) =>
    count === 1
      ? "This action will change this task."
      : `This action will change these ${count} tasks.`,
};


export function ApprovalCard({
  card,
  deciding,
  pendingDecision,
  error,
  onDecide,
}: ApprovalCardProps) {
  const tasks = affectedTasks(card);
  const heading = ACTION_HEADINGS[card.tool_name] ?? "Confirm this action";
  const consequence = CONSEQUENCES[card.tool_name]?.(tasks.length);

  return (
    <section
      aria-label="Approval required"
      // `role="alert"` rather than a dialog. A dialog would be a thing the user
      // opens; this is a thing that is already open and cannot be dismissed
      // without answering it.
      role="alert"
      data-slot="approval-card"
      className="flex flex-col gap-3 border border-destructive/60 bg-destructive/5 p-4"
    >
      <div className="flex flex-col gap-1">
        <p className="text-sm font-semibold tracking-wide uppercase text-destructive">
          Approval required
        </p>
        <p className="text-base font-semibold">{heading}</p>
      </div>

      {/*
        The affected tasks, from the server preview, always expanded. This list
        is the reason the card exists: it is what the user is answering about.
      */}
      {tasks.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {tasks.map((task) => (
            <li
              key={task.id}
              className="border bg-background px-3 py-2 text-sm"
            >
              {task.title}
            </li>
          ))}
        </ul>
      ) : (
        // The server said an approval is pending but named no task. Saying so is
        // better than rendering an empty box that reads as "nothing will happen".
        <p className="text-sm text-muted-foreground">
          The server did not report any affected tasks for this approval.
        </p>
      )}

      {consequence && (
        <p className="text-sm text-muted-foreground">{consequence}</p>
      )}

      {error && (
        <p role="status" className="text-sm font-medium text-destructive">
          {error}
        </p>
      )}

      <div className="flex items-center gap-2">
        {/*
          Both buttons disable together while either decision is in flight. A
          second click cannot submit a second decision or a second continuation.
          The server refuses a second decision regardless, through a guarded
          update, so this is the courtesy and that is the guarantee.
        */}
        <Button
          type="button"
          variant="outline"
          disabled={deciding}
          onClick={() => onDecide("denied")}
        >
          {deciding && pendingDecision === "denied" ? "Rejecting…" : "Reject"}
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={deciding}
          onClick={() => onDecide("approved")}
        >
          {deciding && pendingDecision === "approved"
            ? "Approving…"
            : "Approve"}
        </Button>
        {deciding && (
          <span
            role="status"
            className="text-sm text-muted-foreground"
            data-slot="approval-card-progress"
          >
            Waiting for the server to commit this decision…
          </span>
        )}
      </div>
    </section>
  );
}
