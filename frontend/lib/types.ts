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
