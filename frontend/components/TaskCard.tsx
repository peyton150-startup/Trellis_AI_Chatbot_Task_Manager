import { TaskHistory } from "./TaskHistory";
import type { Task } from "../lib/types";

const dueDateFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

const timestampFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

function formatDueDate(value: string | null): string {
  if (value === null) {
    return "No due date";
  }
  return dueDateFormatter.format(new Date(`${value}T00:00:00Z`));
}

export interface TaskCardProps {
  task: Task;
  blockedByTitle: string | null;
}

export function TaskCard({ task, blockedByTitle }: TaskCardProps) {
  return (
    <article className="task-card" data-priority={task.priority}>
      <header className="task-card__header">
        <span className="task-card__status">{task.status}</span>
        <span className="task-card__priority">{task.priority}</span>
      </header>

      <h2>{task.title}</h2>
      <p className={task.notes ? "task-card__notes" : "task-card__notes muted"}>
        {task.notes || "No notes"}
      </p>

      <dl className="task-card__facts">
        <div>
          <dt>Due</dt>
          <dd>
            {task.due_date === null ? (
              "No due date"
            ) : (
              <time dateTime={task.due_date}>{formatDueDate(task.due_date)}</time>
            )}
          </dd>
        </div>
        <div>
          <dt>Version</dt>
          <dd>v{task.version}</dd>
        </div>
      </dl>

      {blockedByTitle !== null ? (
        <p className="task-card__blocked">
          Blocked by <strong>{blockedByTitle}</strong>
        </p>
      ) : null}

      <details className="task-card__record">
        <summary>Record details</summary>
        <dl>
          <div>
            <dt>Task ID</dt>
            <dd>{task.id}</dd>
          </div>
          <div>
            <dt>Owner ID</dt>
            <dd>{task.owner_id}</dd>
          </div>
          <div>
            <dt>Created</dt>
            <dd>
              <time dateTime={task.created_at}>
                {timestampFormatter.format(new Date(task.created_at))}
              </time>
            </dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>
              <time dateTime={task.updated_at}>
                {timestampFormatter.format(new Date(task.updated_at))}
              </time>
            </dd>
          </div>
        </dl>
      </details>

      <TaskHistory
        key={`${task.id}:${task.version}`}
        taskId={task.id}
        currentVersion={task.version}
      />
    </article>
  );
}
