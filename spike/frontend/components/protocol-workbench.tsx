"use client";

import { useAgUiInterrupts } from "@assistant-ui/react-ag-ui";
import { useAuiState } from "@assistant-ui/react";
import { useEffect, useMemo, useState } from "react";
import { ChatThread } from "./chat-thread";
import { RuntimeProvider } from "./runtime-provider";

type SpikeState = {
  execution_count: number;
  executions: Array<{ tool_call_id: string; item_id: string }>;
  expected_item_id: string;
  expected_tool_call_id: string;
  requests_seen: Array<{
    method: string;
    path: string;
    body: { resume?: unknown[] };
  }>;
};

const STATE_URL =
  process.env.NEXT_PUBLIC_STATE_URL ?? "http://127.0.0.1:8000/spike-state";

function ProtocolRail() {
  const interrupts = useAgUiInterrupts();
  const messages = useAuiState((state) => state.thread.messages);
  const isRunning = useAuiState((state) => state.thread.isRunning);
  const [serverState, setServerState] = useState<SpikeState | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      const response = await fetch(STATE_URL, { cache: "no-store" });
      const next = (await response.json()) as SpikeState;
      if (active) setServerState(next);
    };
    void load();
    const timer = window.setInterval(() => void load(), 400);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const evidence = useMemo(() => {
    const userSent = messages.some((message) => message.role === "user");
    const assistantParts = messages
      .filter((message) => message.role === "assistant")
      .flatMap((message) => message.content);
    const streamed = assistantParts.some(
      (part) => part.type === "text" && part.text.length > 0,
    );
    const toolObserved = assistantParts.some((part) => part.type === "tool-call");
    const resumed = (serverState?.requests_seen.length ?? 0) > 1;
    return { userSent, streamed, toolObserved, resumed };
  }, [messages, serverState]);

  const stages = [
    {
      number: "01",
      name: "Message",
      detail: "assistant-ui → POST /ag-ui",
      done: evidence.userSent,
    },
    {
      number: "02",
      name: "Stream",
      detail: "Pydantic AI → AG-UI events",
      done: evidence.streamed || evidence.toolObserved,
    },
    {
      number: "03",
      name: "Interrupt",
      detail: "Renderable approval payload",
      done: interrupts.length > 0 || evidence.resumed,
    },
    {
      number: "04",
      name: "Resume",
      detail: "Decision → original tool call",
      done: evidence.resumed,
    },
  ];

  return (
    <aside className="protocol-rail" aria-label="Live protocol evidence">
      <div className="rail-heading">
        <span>Live protocol</span>
        <span className={`connection ${isRunning ? "connection--busy" : ""}`}>
          {isRunning ? "Streaming" : "Ready"}
        </span>
      </div>
      <div className="stage-list">
        {stages.map((stage) => (
          <div className={`stage ${stage.done ? "stage--done" : ""}`} key={stage.number}>
            <span className="stage-number">{stage.number}</span>
            <div>
              <strong>{stage.name}</strong>
              <p>{stage.detail}</p>
            </div>
            <span className="stage-status">{stage.done ? "PASS" : "WAIT"}</span>
          </div>
        ))}
      </div>
      <div className="server-proof">
        <span className="server-proof__label">Tool body executions</span>
        <strong data-testid="execution-count">
          {serverState?.execution_count ?? 0}
        </strong>
        <p>
          Approve must become 1. Deny must remain 0 after a reset.
        </p>
      </div>
      <div className="request-proof">
        <span>Requests captured</span>
        <code data-testid="request-count">
          {serverState?.requests_seen.length ?? 0}
        </code>
      </div>
    </aside>
  );
}

function WorkbenchContent() {
  return (
    <main className="workbench-shell">
      <header className="topbar">
        <div className="brand-block">
          <span className="brand-mark">T</span>
          <div>
            <strong>Trellis</strong>
            <span>Gate A workbench</span>
          </div>
        </div>
        <div className="build-meta">
          <span>Disposable spike</span>
          <code>T00A · Node 22+ · Python 3.12</code>
        </div>
      </header>
      <section className="intro-band">
        <div>
          <span className="kicker">Integration proof 01</span>
          <h1>Can an approval survive the wire?</h1>
        </div>
        <p>
          One narrow question, instrumented end to end. This code is evidence,
          not the product foundation.
        </p>
      </section>
      <div className="workbench-grid">
        <section className="chat-panel" aria-label="AG-UI conversation">
          <ChatThread />
        </section>
        <ProtocolRail />
      </div>
    </main>
  );
}

export function ProtocolWorkbench() {
  return (
    <RuntimeProvider>
      <WorkbenchContent />
    </RuntimeProvider>
  );
}
