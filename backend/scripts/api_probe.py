from __future__ import annotations

import inspect
import json
import sys
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

from ag_ui.core import ResumeEntry
from pydantic_ai import (
    Agent,
    ApprovalRequired,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolApproved,
    ToolDenied,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.ui.ag_ui import AGUIAdapter


CALL_ID = "probe-call-42"


@dataclass(frozen=True)
class ProbeResult:
    number: int
    name: str
    detail: str


def make_model_response(task_ids: list[str]):
    def model_response(
        messages: list[ModelMessage], _info: AgentInfo
    ) -> ModelResponse:
        if any(
            isinstance(part, ToolReturnPart)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        ):
            return ModelResponse(parts=[TextPart("tool completed")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="bulk_update_tasks",
                    args={"task_ids": task_ids},
                    tool_call_id=CALL_ID,
                )
            ]
        )

    return model_response


def make_conditional_agent(
    executions: list[tuple[str, tuple[str, ...]]],
    task_ids: list[str],
) -> Agent[None, str | DeferredToolRequests]:
    agent = Agent(
        FunctionModel(make_model_response(task_ids), model_name="trellis-api-probe"),
        output_type=[str, DeferredToolRequests],
    )

    @agent.tool
    def bulk_update_tasks(ctx: RunContext[None], task_ids: list[str]) -> str:
        if len(task_ids) > 3 and not ctx.tool_call_approved:
            raise ApprovalRequired(metadata={"reason": "blast_radius"})
        executions.append((ctx.tool_call_id, tuple(task_ids)))
        return f"updated {len(task_ids)} tasks"

    return agent


def probe_imports() -> ProbeResult:
    expected = {
        "DeferredToolRequests": DeferredToolRequests,
        "DeferredToolResults": DeferredToolResults,
        "ToolApproved": ToolApproved,
        "ToolDenied": ToolDenied,
    }
    for expected_name, imported_type in expected.items():
        assert imported_type.__name__ == expected_name
        assert imported_type.__module__ in {
            "pydantic_ai._deferred",
            "pydantic_ai.tools",
        }
    return ProbeResult(
        1,
        "deferred approval imports",
        "top-level pydantic_ai exports: " + ", ".join(expected),
    )


def probe_static_approval_parameter() -> ProbeResult:
    parameter = inspect.signature(Agent.tool_plain).parameters.get("requires_approval")
    assert parameter is not None
    assert parameter.default is False

    executions: list[str] = []
    agent = Agent(
        FunctionModel(
            make_model_response(["a", "b", "c", "d"]),
            model_name="trellis-static-approval-probe",
        ),
        output_type=[str, DeferredToolRequests],
    )

    @agent.tool_plain(name="bulk_update_tasks", requires_approval=True)
    def static_approval(task_ids: list[str]) -> str:
        executions.append(",".join(task_ids))
        return "updated"

    first = agent.run_sync("update four tasks")
    assert isinstance(first.output, DeferredToolRequests)
    assert first.output.approvals[0].tool_call_id == CALL_ID
    assert executions == []
    return ProbeResult(
        2,
        "static approval registration",
        "@agent.tool_plain(requires_approval=True) defers before the tool body",
    )


def probe_message_history_round_trip() -> ProbeResult:
    messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("hello")]),
        ModelResponse(parts=[TextPart("world")], model_name="probe"),
    ]
    serialized = ModelMessagesTypeAdapter.dump_json(messages)
    json_value = json.loads(serialized)
    restored = ModelMessagesTypeAdapter.validate_json(serialized)
    assert isinstance(json_value, list)
    assert restored == messages
    return ProbeResult(
        3,
        "message history round trip",
        "ModelMessagesTypeAdapter.dump_json and validate_json preserve ModelMessage values",
    )


def probe_conditional_approval() -> tuple[ProbeResult, Agent[Any, Any], list[ModelMessage]]:
    below_threshold_executions: list[tuple[str, tuple[str, ...]]] = []
    below_threshold_agent = make_conditional_agent(
        below_threshold_executions, ["a", "b", "c"]
    )
    below_threshold_result = below_threshold_agent.run_sync("update three tasks")
    assert below_threshold_result.output == "tool completed"
    assert below_threshold_executions == [(CALL_ID, ("a", "b", "c"))]

    executions: list[tuple[str, tuple[str, ...]]] = []
    agent = make_conditional_agent(executions, ["a", "b", "c", "d"])
    first = agent.run_sync("update four tasks")
    assert isinstance(first.output, DeferredToolRequests)
    assert executions == []
    assert len(first.output.approvals) == 1
    assert first.output.approvals[0].tool_call_id == CALL_ID

    history = first.all_messages()
    resumed = agent.run_sync(
        message_history=history,
        deferred_tool_results=DeferredToolResults(
            approvals={CALL_ID: ToolApproved()}
        ),
    )
    assert resumed.output == "tool completed"
    assert executions == [(CALL_ID, ("a", "b", "c", "d"))]
    return (
        ProbeResult(
            4,
            "conditional approval",
            "requires_approval is bool-only; raise ApprovalRequired in the tool and check ctx.tool_call_approved",
        ),
        agent,
        history,
    )


def build_ag_ui_adapter(
    agent: Agent[Any, Any], *, approved: bool
) -> AGUIAdapter[Any, Any, Any, Any, Any]:
    payload = {
        "threadId": "probe-thread",
        "runId": "probe-continuation",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
        "resume": [
            {
                "interruptId": f"int-{CALL_ID}",
                "status": "resolved",
                "payload": {"approved": approved},
            }
        ],
    }
    run_input = AGUIAdapter.build_run_input(json.dumps(payload).encode())
    assert isinstance(run_input.resume, list)
    assert isinstance(run_input.resume[0], ResumeEntry)
    return AGUIAdapter(agent=agent, run_input=run_input)


def probe_ag_ui_resume_shape(agent: Agent[Any, Any]) -> ProbeResult:
    approved_adapter = build_ag_ui_adapter(agent, approved=True)
    approved_results = approved_adapter.deferred_tool_results
    assert approved_results is not None
    assert isinstance(approved_results.approvals[CALL_ID], ToolApproved)

    denied_adapter = build_ag_ui_adapter(agent, approved=False)
    denied_results = denied_adapter.deferred_tool_results
    assert denied_results is not None
    assert isinstance(denied_results.approvals[CALL_ID], ToolDenied)
    return ProbeResult(
        5,
        "AG-UI resume shape",
        "POST RunAgentInput uses resume=[{interruptId, status='resolved', payload={approved: bool}}]",
    )


def probe_tool_call_identity(
    agent: Agent[Any, Any], history: list[ModelMessage]
) -> ProbeResult:
    call_ids = [
        part.tool_call_id
        for message in history
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]
    assert call_ids == [CALL_ID]

    adapter = build_ag_ui_adapter(agent, approved=True)
    deferred_results = adapter.deferred_tool_results
    assert deferred_results is not None
    assert list(deferred_results.approvals) == [CALL_ID]
    assert f"int-{CALL_ID}" != CALL_ID
    return ProbeResult(
        6,
        "tool call identity",
        f"interrupt id int-{CALL_ID} maps back to original tool_call_id {CALL_ID} on continuation",
    )


def run_probe() -> list[ProbeResult]:
    results = [
        probe_imports(),
        probe_static_approval_parameter(),
        probe_message_history_round_trip(),
    ]
    conditional_result, agent, history = probe_conditional_approval()
    results.append(conditional_result)
    results.append(probe_ag_ui_resume_shape(agent))
    results.append(probe_tool_call_identity(agent, history))
    return results


def main() -> int:
    print("Trellis T00 Pydantic AI API probe")
    print(f"Python {sys.version.split()[0]}")
    print(f"pydantic-ai {version('pydantic-ai')}")
    print(f"pydantic-ai-slim {version('pydantic-ai-slim')}")
    print(f"ag-ui-protocol {version('ag-ui-protocol')}")
    print()
    try:
        results = run_probe()
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1

    for result in results:
        print(f"PASS {result.number}/6 {result.name}: {result.detail}")
    print("ALL 6 API FACTS CONFIRMED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
