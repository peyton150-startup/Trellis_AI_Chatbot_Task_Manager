"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { fetchTasks } from "./api";
import type { Task } from "./types";

export interface BoardState {
  tasks: Task[];
  isLoading: boolean;
  error: string | null;
  lastRefreshedAt: number | null;
  refetch: () => Promise<void>;
}

export function useBoard(): BoardState {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<number | null>(null);
  const activeRequest = useRef<AbortController | null>(null);

  const refetch = useCallback(async () => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setIsLoading(true);
    setError(null);

    try {
      const response = await fetchTasks({ signal: controller.signal });
      if (activeRequest.current === controller) {
        setTasks(response.tasks);
        setLastRefreshedAt(Date.now());
      }
    } catch (requestError) {
      if (requestError instanceof DOMException && requestError.name === "AbortError") {
        return;
      }
      if (activeRequest.current === controller) {
        setError(
          requestError instanceof Error
            ? requestError.message
            : "The board could not be loaded.",
        );
      }
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setIsLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void refetch();
    return () => activeRequest.current?.abort();
  }, [refetch]);

  return { tasks, isLoading, error, lastRefreshedAt, refetch };
}
