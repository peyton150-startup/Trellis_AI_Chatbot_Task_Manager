"use client";

import { useState } from "react";
import type { SyntheticEvent } from "react";

import { fetchTaskHistory } from "../lib/api";
import type {
  TaskHistoryChange,
  TaskHistoryEntry,
  TaskHistoryField,
} from "../lib/types";

const historyTimestampFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

const historyDueDateFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

const fieldLabels: Record<TaskHistoryField, string> = {
  title: "Title",
  notes: "Notes",
  due_date: "Due date",
  priority: "Priority",
  status: "Status",
  blocked_by: "Blocked by",
};

function capitalize(value: string): string {
  return value.length === 0 ? value : value[0].toUpperCase() + value.slice(1);
}

function formatChangeValue(
  change: TaskHistoryChange,
  value: string | null,
): string {
  if (value === null) {
    return change.field === "blocked_by" ? "Not blocked" : "None";
  }

  if (change.field === "due_date") {
    return historyDueDateFormatter.format(new Date(`${value}T00:00:00Z`));
  }

  if (change.field === "priority" || change.field === "status") {
    return capitalize(value);
  }

  if (change.field === "blocked_by") {
    return `Task ${value}`;
  }

  return value;
}

function eventLabel(entry: TaskHistoryEntry): string {
  if (entry.operation === "restored") {
    if (entry.effect === "created") {
      return "Restored task";
    }
    if (entry.effect === "deleted") {
      return "Undo removed task";
    }
    return "Restored previous values";
  }
  return capitalize(entry.effect);
}

function versionLabel(entry: TaskHistoryEntry): string {
  if (
    entry.version_before !== null &&
    entry.version_after !== null &&
    entry.version_before !== entry.version_after
  ) {
    return `v${entry.version_before} ??? v${entry.version_after}`;
  }
  if (entry.version_after !== null) {
    return `v${entry.version_after}`;
  }
  if (entry.version_before !== null) {
    return `v${entry.version_before}`;
  }
  return "No version";
}

export interface TaskHistoryProps {
  taskId: string;
  currentVersion: number;
}

export function TaskHistory({ taskId, currentVersion }: TaskHistoryProps) {
  const [entries, setEntries] = useState<TaskHistoryEntry[]>([]);
  const [nextBeforeEventId, setNextBeforeEventId] = useState<number | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function loadInitial(): Promise<void> {
    if (loading) {
      return;
    }

    setLoading(true);
    setError(null);
    try {
      const history = await fetchTaskHistory(taskId);
      setEntries(history.entries);
      setNextBeforeEventId(history.next_before_event_id);
      setHasLoaded(true);
    } catch {
      setError("History could not be loaded.");
    } finally {
      setLoading(false);
    }
  }

  async function loadOlder(): Promise<void> {
    if (nextBeforeEventId === null || loadingOlder) {
      return;
    }

    setLoadingOlder(true);
    setError(null);
    try {
      const history = await fetchTaskHistory(taskId, {
        beforeEventId: nextBeforeEventId,
      });
      setEntries((current) => [...current, ...history.entries]);
      setNextBeforeEventId(history.next_before_event_id);
    } catch {
      setError("Older history could not be loaded.");
    } finally {
      setLoadingOlder(false);
    }
  }

  function handleToggle(event: SyntheticEvent<HTMLDetailsElement>): void {
    if (event.currentTarget.open && !hasLoaded && !loading) {
      void loadInitial();
    }
  }

  const chronologicalEntries = [...entries].reverse();

  return (
    <details className="task-card__record" onToggle={handleToggle}>
      <summary>History</summary>

      {loading ? <p className="muted">Loading history???</p> : null}

      {!loading && error !== null ? (
        <div>
          <p className="muted">{error}</p>
          {!hasLoaded ? (
            <button type="button" onClick={() => void loadInitial()}>
              Retry
            </button>
          ) : null}
        </div>
      ) : null}

      {!loading && hasLoaded && entries.length === 0 ? (
        <p className="muted">
          No recorded changes yet. Current version: v{currentVersion}.
        </p>
      ) : null}

      {chronologicalEntries.length > 0 ? (
        <ol>
          {chronologicalEntries.map((entry) => (
            <li key={entry.event_id}>
              <p>
                <strong>{eventLabel(entry)}</strong>{" "}
                <span className="muted">{versionLabel(entry)}</span>
              </p>
              <p className="muted">
                <time dateTime={entry.occurred_at}>
                  {historyTimestampFormatter.format(new Date(entry.occurred_at))}
                </time>
              </p>

              {entry.changes.length > 0 ? (
                <dl>
                  {entry.changes.map((change) => (
                    <div key={change.field}>
                      <dt>{fieldLabels[change.field]}</dt>
                      <dd>
                        {formatChangeValue(change, change.before)}
                        {" ??? "}
                        {formatChangeValue(change, change.after)}
                      </dd>
                    </div>
                  ))}
                </dl>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}

      {hasLoaded && nextBeforeEventId !== null ? (
        <button
          type="button"
          disabled={loadingOlder}
          onClick={() => void loadOlder()}
        >
          {loadingOlder ? "Loading???" : "Load older"}
        </button>
      ) : null}
    </details>
  );
}
