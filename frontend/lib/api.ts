import type { TaskHistoryResponse, TasksResponse } from "./types";
import type { ApprovalDecision, RunDetail } from "./useRun";
import { withNgrokBypassHeaders } from "./ngrokDemoIngress";

const TASKS_PATH = "/api/tasks";
const RUNS_PATH = "/api/runs";

export async function fetchTasks(init?: RequestInit): Promise<TasksResponse> {
  const response = await fetch(TASKS_PATH, {
    ...init,
    cache: "no-store",
    headers: withNgrokBypassHeaders(init?.headers),
  });

  if (!response.ok) {
    throw new Error(`Task request failed with HTTP ${response.status}`);
  }

  return (await response.json()) as TasksResponse;
}


export interface FetchTaskHistoryOptions {
  limit?: number;
  beforeEventId?: number;
}

export async function fetchTaskHistory(
  taskId: string,
  options: FetchTaskHistoryOptions = {},
  init?: RequestInit,
): Promise<TaskHistoryResponse> {
  const search = new URLSearchParams();

  if (options.limit !== undefined) {
    search.set("limit", String(options.limit));
  }
  if (options.beforeEventId !== undefined) {
    search.set("before_event_id", String(options.beforeEventId));
  }

  const queryString = search.toString();
  const query = queryString ? `?${queryString}` : "";
  const path = `${TASKS_PATH}/${encodeURIComponent(taskId)}/history${query}`;

  const response = await fetch(path, {
    ...init,
    cache: "no-store",
    headers: withNgrokBypassHeaders(init?.headers),
  });

  if (!response.ok) {
    throw new Error(`Task history request failed with HTTP ${response.status}`);
  }

  return (await response.json()) as TaskHistoryResponse;
}

/**
 * T16. The run record, including the authoritative pending approval card.
 *
 * The browser has no other way to learn what a pending approval covers.
 * `pending_approval.preview` is built server side in `agent._open_approval`
 * after `policy.resolve_scope` has run, and the AG-UI interrupt carries no
 * equivalent. Reconstructing the card from the tool call arguments the stream
 * happens to expose would be the client deriving the preview, which section 12
 * of BUILD_SPEC forbids.
 *
 * Goes through the same relative path and the same ngrok bypass header as
 * `fetchTasks`, so D-61's same-origin rewrite and T14I's free-plan bypass cover
 * this third browser transport without a second rule.
 */
export async function fetchRun(
  runId: string,
  init?: RequestInit,
): Promise<RunDetail> {
  const response = await fetch(`${RUNS_PATH}/${encodeURIComponent(runId)}`, {
    ...init,
    cache: "no-store",
    headers: withNgrokBypassHeaders(init?.headers),
  });

  if (!response.ok) {
    throw new Error(`Run request failed with HTTP ${response.status}`);
  }

  return (await response.json()) as RunDetail;
}

/**
 * T16. Record the human decision against the server's own approval record.
 *
 * This is the request the shipped approval UI never made. It persists the
 * decision and returns the refreshed `RunDetail`; it does not execute the tool.
 * The framework continuation is a separate `POST /api/agui` carrying `resume[]`,
 * and it reads the row this call wrote. See D-58.
 *
 * The body carries exactly one field. `ApprovalDecisionRequest` forbids extra
 * keys, so adding the run id, the call id, or the preview here would be a 422
 * rather than a convenience.
 */
export async function postApprovalDecision(
  runId: string,
  toolCallId: string,
  decision: ApprovalDecision,
  init?: RequestInit,
): Promise<RunDetail> {
  const path =
    `${RUNS_PATH}/${encodeURIComponent(runId)}` +
    `/approvals/${encodeURIComponent(toolCallId)}`;

  const response = await fetch(path, {
    ...init,
    method: "POST",
    cache: "no-store",
    headers: withNgrokBypassHeaders({
      ...(init?.headers as Record<string, string> | undefined),
      "Content-Type": "application/json",
    }),
    body: JSON.stringify({ decision }),
  });

  if (!response.ok) {
    throw new Error(
      `Approval decision failed with HTTP ${response.status}`,
    );
  }

  return (await response.json()) as RunDetail;
}
