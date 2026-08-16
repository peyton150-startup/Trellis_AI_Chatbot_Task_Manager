import type { TasksResponse } from "./types";
import { withNgrokBypassHeaders } from "./ngrokDemoIngress";

const TASKS_PATH = "/api/tasks";

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
