"use client";

import { TaskCard } from "./TaskCard";
import { useBoard } from "../lib/useBoard";

export function Board() {
  const { tasks, isLoading, error, refetch } = useBoard();

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

  return (
    <section className="board" aria-label="Task board">
      <header className="board__toolbar">
        <p>
          <strong>{tasks.length}</strong> committed tasks
          <span aria-hidden="true"> / </span>
          <span>{openCount} open</span>
        </p>
        <button
          type="button"
          onClick={() => void refetch()}
          disabled={isLoading}
        >
          {isLoading ? "Refreshing..." : "Refresh board"}
        </button>
      </header>

      {error !== null ? (
        <p className="board__stale" role="alert">
          Showing the last committed board. Refresh failed: {error}
        </p>
      ) : null}

      <div className="board__grid">
        {tasks.map((task) => (
          <TaskCard key={task.id} task={task} />
        ))}
      </div>
    </section>
  );
}
