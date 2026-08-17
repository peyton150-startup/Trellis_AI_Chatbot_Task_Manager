"use client";

import { useState } from "react";

import { TaskCard } from "./TaskCard";
import type { BoardState } from "../lib/useBoard";
import type { Task } from "../lib/types";

type SortMode =
  | "default"
  | "priority-highest"
  | "priority-lowest"
  | "name-az"
  | "name-za"
  | "due-soonest"
  | "due-latest"
  | "status-open-first"
  | "status-done-first";

const PRIORITY_RANK: Record<Task["priority"], number> = {
  low: 0,
  medium: 1,
  high: 2,
  critical: 3,
};

const dueDateKey = (dueDate: string | null) =>
  dueDate === null ? null : dueDate.slice(0, 10);

function compareTitleAndId(a: Task, b: Task) {
  const titleComparison = a.title.localeCompare(b.title, undefined, {
    sensitivity: "base",
  });

  if (titleComparison !== 0) {
    return titleComparison;
  }

  return a.id.localeCompare(b.id);
}

function compareTasks(a: Task, b: Task, sortBy: SortMode) {
  let comparison = 0;

  switch (sortBy) {
    case "priority-highest":
      comparison = PRIORITY_RANK[b.priority] - PRIORITY_RANK[a.priority];
      break;

    case "priority-lowest":
      comparison = PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority];
      break;

    case "name-az":
      return compareTitleAndId(a, b);

    case "name-za": {
      const titleComparison = b.title.localeCompare(a.title, undefined, {
        sensitivity: "base",
      });

      return titleComparison !== 0
        ? titleComparison
        : a.id.localeCompare(b.id);
    }

    case "due-soonest":
    case "due-latest": {
      const aDueDate = dueDateKey(a.due_date);
      const bDueDate = dueDateKey(b.due_date);

      // Tasks without due dates remain last in either direction.
      if (aDueDate === null && bDueDate !== null) {
        return 1;
      }

      if (aDueDate !== null && bDueDate === null) {
        return -1;
      }

      if (aDueDate !== null && bDueDate !== null) {
        comparison =
          sortBy === "due-soonest"
            ? aDueDate.localeCompare(bDueDate)
            : bDueDate.localeCompare(aDueDate);
      }

      break;
    }

    case "status-open-first":
      comparison =
        Number(a.status === "done") - Number(b.status === "done");
      break;

    case "status-done-first":
      comparison =
        Number(a.status === "open") - Number(b.status === "open");
      break;

    case "default":
      return 0;
  }

  return comparison !== 0 ? comparison : compareTitleAndId(a, b);
}

interface BoardProps {
  state: BoardState;
}

export function Board({ state }: BoardProps) {
  const { tasks, isLoading, error, lastRefreshedAt, refetch } = state;

  const [filtersOpen, setFiltersOpen] = useState(false);
  const [priorityFilter, setPriorityFilter] = useState("all");
  const [dueFilter, setDueFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [blockedFilter, setBlockedFilter] = useState("all");
  const [sortBy, setSortBy] = useState<SortMode>("default");

  if (isLoading && tasks.length === 0) {
    return <p className="board-state">Loading committed tasks...</p>;
  }

  if (error !== null && tasks.length === 0) {
    return (
      <section className="board-state board-state--error" role="alert">
        <p>{error}</p>

        <button type="button" onClick={() => void refetch()}>
          Try again
        </button>
      </section>
    );
  }

  if (tasks.length === 0) {
    return (
      <section className="board-state">
        <p>No committed tasks are on the board.</p>

        <button type="button" onClick={() => void refetch()}>
          Refresh board
        </button>
      </section>
    );
  }

  const openCount = tasks.filter((task) => task.status === "open").length;

  const taskTitlesById = new Map(
    tasks.map((task) => [task.id, task.title] as const),
  );

  const localDateKey = (date: Date) =>
    [
      date.getFullYear(),
      String(date.getMonth() + 1).padStart(2, "0"),
      String(date.getDate()).padStart(2, "0"),
    ].join("-");

  const now = new Date();
  const today = localDateKey(now);

  const sevenDaysFromNow = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate() + 7,
  );

  const nextSevenDays = localDateKey(sevenDaysFromNow);

  const filteredTasks = tasks.filter((task) => {
    if (priorityFilter !== "all" && task.priority !== priorityFilter) {
      return false;
    }

    if (statusFilter !== "all" && task.status !== statusFilter) {
      return false;
    }

    if (blockedFilter === "blocked" && task.blocked_by === null) {
      return false;
    }

    if (blockedFilter === "unblocked" && task.blocked_by !== null) {
      return false;
    }

    const dueDate = dueDateKey(task.due_date);

    if (dueFilter === "overdue") {
      return (
        dueDate !== null &&
        dueDate < today &&
        task.status !== "done"
      );
    }

    if (dueFilter === "today") {
      return dueDate === today;
    }

    if (dueFilter === "next-seven-days") {
      return (
        dueDate !== null &&
        dueDate >= today &&
        dueDate <= nextSevenDays
      );
    }

    if (dueFilter === "no-due-date") {
      return dueDate === null;
    }

    return true;
  });

  const sortedTasks =
    sortBy === "default"
      ? filteredTasks
      : [...filteredTasks].sort((a, b) => compareTasks(a, b, sortBy));

  const activeFilterCount = [
    priorityFilter,
    dueFilter,
    statusFilter,
    blockedFilter,
  ].filter((value) => value !== "all").length;

  const hasViewChanges =
    activeFilterCount > 0 || sortBy !== "default";

  const resetView = () => {
    setPriorityFilter("all");
    setDueFilter("all");
    setStatusFilter("all");
    setBlockedFilter("all");
    setSortBy("default");
  };

  return (
    <section className="board" aria-label="Task board">
      <header className="board__toolbar">
        <div className="board__summary">
          <button
            type="button"
            className="board__filter-toggle"
            aria-expanded={filtersOpen}
            aria-controls="board-filters"
            onClick={() => setFiltersOpen((open) => !open)}
          >
            Filter / Sort
            {activeFilterCount > 0 ? ` (${activeFilterCount})` : ""}
            {sortBy !== "default" ? " - Sorted" : ""}
          </button>

          <p>
            <strong>{tasks.length}</strong> committed tasks
            <span aria-hidden="true"> / </span>
            <span>{openCount} open</span>
          </p>
        </div>

        <div className="board__toolbar-actions">
          {lastRefreshedAt !== null ? (
            <span
              className="board__refresh-status"
              role="status"
              aria-live="polite"
            >
              Refreshed{" "}
              {new Date(lastRefreshedAt).toLocaleTimeString([], {
                hour: "numeric",
                minute: "2-digit",
                second: "2-digit",
              })}
            </span>
          ) : null}

          <button
            type="button"
            onClick={() => void refetch()}
            disabled={isLoading}
          >
            {isLoading ? "Refreshing..." : "Refresh board"}
          </button>
        </div>
      </header>

      {filtersOpen ? (
        <div className="board__filters" id="board-filters">
          <label>
            <span>Priority</span>

            <select
              value={priorityFilter}
              onChange={(event) =>
                setPriorityFilter(event.target.value)
              }
            >
              <option value="all">All priorities</option>
              <option value="critical">Critical</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
          </label>

          <label>
            <span>Due</span>

            <select
              value={dueFilter}
              onChange={(event) => setDueFilter(event.target.value)}
            >
              <option value="all">Any date</option>
              <option value="overdue">Overdue</option>
              <option value="today">Due today</option>
              <option value="next-seven-days">Next 7 days</option>
              <option value="no-due-date">No due date</option>
            </select>
          </label>

          <label>
            <span>Status</span>

            <select
              value={statusFilter}
              onChange={(event) =>
                setStatusFilter(event.target.value)
              }
            >
              <option value="all">All statuses</option>
              <option value="open">Open</option>
              <option value="done">Done</option>
            </select>
          </label>

          <label>
            <span>Dependency</span>

            <select
              value={blockedFilter}
              onChange={(event) =>
                setBlockedFilter(event.target.value)
              }
            >
              <option value="all">Any dependency</option>
              <option value="blocked">Blocked</option>
              <option value="unblocked">Unblocked</option>
            </select>
          </label>

          <label>
            <span>Sort by</span>

            <select
              value={sortBy}
              onChange={(event) =>
                setSortBy(event.target.value as SortMode)
              }
            >
              <option value="default">Default order</option>
              <option value="priority-highest">
                Priority - highest first
              </option>
              <option value="priority-lowest">
                Priority - lowest first
              </option>
              <option value="name-az">Name - A to Z</option>
              <option value="name-za">Name - Z to A</option>
              <option value="due-soonest">
                Due date - soonest first
              </option>
              <option value="due-latest">
                Due date - latest first
              </option>
              <option value="status-open-first">
                Status - Open first
              </option>
              <option value="status-done-first">
                Status - Done first
              </option>
            </select>
          </label>

          <div className="board__filter-footer">
            <span>
              Showing <strong>{sortedTasks.length}</strong> of{" "}
              <strong>{tasks.length}</strong>
            </span>

            <button
              type="button"
              onClick={resetView}
              disabled={!hasViewChanges}
            >
              Reset view
            </button>
          </div>
        </div>
      ) : null}

      {error !== null ? (
        <p className="board__stale" role="alert">
          Showing the last committed board. Refresh failed: {error}
        </p>
      ) : null}

      <div className="board__grid">
        {sortedTasks.map((task) => (
          <TaskCard
            key={task.id}
            task={task}
            blockedByTitle={
              task.blocked_by === null
                ? null
                : (taskTitlesById.get(task.blocked_by) ??
                  "Unknown task")
            }
          />
        ))}
      </div>

      {sortedTasks.length === 0 ? (
        <p className="board__filter-empty">
          No tasks match the current filters.
        </p>
      ) : null}
    </section>
  );
}
