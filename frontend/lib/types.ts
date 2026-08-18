export type TaskPriority = "low" | "medium" | "high" | "critical";

export type TaskStatus = "open" | "done";

export interface Task {
  id: string;
  owner_id: string;
  title: string;
  notes: string;
  due_date: string | null;
  priority: TaskPriority;
  status: TaskStatus;
  blocked_by: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface TasksResponse {
  tasks: Task[];
}


export type TaskHistoryField =
  | "title"
  | "notes"
  | "due_date"
  | "priority"
  | "status"
  | "blocked_by";

export type TaskHistoryOperation =
  | "created"
  | "updated"
  | "deleted"
  | "restored";

export type TaskHistoryEffect = "created" | "updated" | "deleted";

export interface TaskHistoryChange {
  field: TaskHistoryField;
  before: string | null;
  after: string | null;
}

export interface TaskHistoryEntry {
  event_id: number;
  operation: TaskHistoryOperation;
  effect: TaskHistoryEffect;
  occurred_at: string;
  version_before: number | null;
  version_after: number | null;
  changes: TaskHistoryChange[];
}

export interface TaskHistoryResponse {
  task_id: string;
  exists_now: boolean;
  current_version: number | null;
  entries: TaskHistoryEntry[];
  next_before_event_id: number | null;
}
