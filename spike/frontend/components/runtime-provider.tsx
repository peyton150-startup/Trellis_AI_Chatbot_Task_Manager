"use client";

import { HttpAgent } from "@ag-ui/client";
import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";
import { type ReactNode, useMemo } from "react";

const AG_UI_URL =
  process.env.NEXT_PUBLIC_AG_UI_URL ?? "http://127.0.0.1:8000/ag-ui";

export function RuntimeProvider({ children }: { children: ReactNode }) {
  const agent = useMemo(() => new HttpAgent({ url: AG_UI_URL }), []);
  const runtime = useAgUiRuntime({
    agent,
    onError: (error) => console.error("AG-UI runtime error", error),
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {children}
    </AssistantRuntimeProvider>
  );
}
