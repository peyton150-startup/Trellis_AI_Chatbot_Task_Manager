import type { TasksResponse } from "./types";

const TASKS_PATH = "/api/tasks";

export async function fetchTasks(init?: RequestInit): Promise<TasksResponse> {
  const response = await fetch(TASKS_PATH, { ...init, cache: "no-store" });

  if (!response.ok) {
    throw new Error(`Task request failed with HTTP ${response.status}`);
  }

  return (await response.json()) as TasksResponse;
}
