from __future__ import annotations

import asyncio
import inspect
import json
import sys
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

from ag_ui.core import (
    ResumeEntry,
    RunFinishedEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
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
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
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


def make_stream_model_response(task_ids: list[str]):
    async def stream_model_response(
        messages: list[ModelMessage], _info: AgentInfo
    ):
        if any(
            isinstance(part, ToolReturnPart)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        ):
            yield "tool completed"
            return
        yield {
            0: DeltaToolCall(
                name="bulk_update_tasks",
                json_args=json.dumps({"task_ids": task_ids}),
                tool_call_id=CALL_ID,
            )
        }

    return stream_model_response


def make_conditional_agent(
    executions: list[tuple[str, tuple[str, ...]]],
    task_ids: list[str],
) -> Agent[None, str | DeferredToolRequests]:
    agent = Agent(
        FunctionModel(
            make_model_response(task_ids),
            stream_function=make_stream_model_response(task_ids),
            model_name="trellis-api-probe",
        ),
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
    assert parameter.annotation in {bool, "bool"}

    executions: list[str] = []
    agent = Agent(
        FunctionModel(
            make_model_response(["a", "b", "c", "d"]),
            stream_function=make_stream_model_response(["a", "b", "c", "d"]),
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
    callable_checks: list[list[str]] = []

    def callable_requirement(task_ids: list[str]) -> bool:
        callable_checks.append(task_ids)
        return len(task_ids) > 3

    callable_agent = Agent(
        FunctionModel(
            make_model_response(["a", "b", "c"]),
            stream_function=make_stream_model_response(["a", "b", "c"]),
            model_name="trellis-callable-approval-probe",
        ),
        output_type=[str, DeferredToolRequests],
    )

    @callable_agent.tool_plain(
        name="bulk_update_tasks",
        requires_approval=callable_requirement,  # type: ignore[arg-type]
    )
    def callable_bulk_update(task_ids: list[str]) -> str:
        return f"updated {len(task_ids)} tasks"

    callable_result = callable_agent.run_sync("update three tasks")
    assert isinstance(callable_result.output, DeferredToolRequests)
    assert callable_checks == []

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
            "a callable is treated as a truthy static gate and is never invoked; raise ApprovalRequired in the tool",
        ),
        agent,
        history,
    )


def build_ag_ui_adapter(
    agent: Agent[Any, Any],
    *,
    run_id: str,
    messages: list[dict[str, Any]],
    approved: bool | None = None,
) -> AGUIAdapter[Any, Any, Any, Any, Any]:
    payload = {
        "threadId": "probe-thread",
        "runId": run_id,
        "state": {},
        "messages": messages,
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if approved is not None:
        payload["resume"] = [
            {
                "interruptId": f"int-{CALL_ID}",
                "status": "resolved",
                "payload": {"approved": approved},
            }
        ]
    run_input = AGUIAdapter.build_run_input(json.dumps(payload).encode())
    if approved is not None:
        assert isinstance(run_input.resume, list)
        assert isinstance(run_input.resume[0], ResumeEntry)
    return AGUIAdapter(agent=agent, run_input=run_input)


async def collect_adapter_events(
    adapter: AGUIAdapter[Any, Any, Any, Any, Any],
    *,
    message_history: list[ModelMessage] | None = None,
) -> tuple[list[Any], list[ModelMessage]]:
    captured_history: list[list[ModelMessage]] = []

    async def capture_history(result: Any) -> None:
        captured_history.append(result.all_messages())

    events = [
        event
        async for event in adapter.run_stream(
            message_history=message_history,
            on_complete=capture_history,
        )
    ]
    assert len(captured_history) == 1
    return events, captured_history[0]


async def run_ag_ui_approval_flow(
    *, approved: bool
) -> tuple[list[Any], list[Any], list[tuple[str, tuple[str, ...]]]]:
    executions: list[tuple[str, tuple[str, ...]]] = []
    agent = make_conditional_agent(executions, ["a", "b", "c", "d"])
    first_adapter = build_ag_ui_adapter(
        agent,
        run_id="probe-initial",
        messages=[
            {
                "id": "probe-user-message",
                "role": "user",
                "content": "update four tasks",
            }
        ],
    )
    first_events, history = await collect_adapter_events(first_adapter)
    assert executions == []

    continuation_adapter = build_ag_ui_adapter(
        agent,
        run_id="probe-continuation",
        messages=[],
        approved=approved,
    )
    continuation_events, _ = await collect_adapter_events(
        continuation_adapter,
        message_history=history,
    )
    return first_events, continuation_events, executions


def probe_ag_ui_interrupt_and_continuation() -> tuple[ProbeResult, ProbeResult]:
    approved_first, approved_continuation, approved_executions = asyncio.run(
        run_ag_ui_approval_flow(approved=True)
    )
    denied_first, denied_continuation, denied_executions = asyncio.run(
        run_ag_ui_approval_flow(approved=False)
    )

    for first_events in (approved_first, denied_first):
        initial_finish = next(
            event for event in first_events if isinstance(event, RunFinishedEvent)
        )
        assert initial_finish.outcome is not None
        assert initial_finish.outcome.type == "interrupt"
        assert len(initial_finish.outcome.interrupts) == 1
        interrupt = initial_finish.outcome.interrupts[0]
        assert interrupt.id == f"int-{CALL_ID}"
        assert interrupt.tool_call_id == CALL_ID
        assert any(
            isinstance(event, ToolCallStartEvent)
            and event.tool_call_id == CALL_ID
            for event in first_events
        )

    for continuation_events in (approved_continuation, denied_continuation):
        continuation_finish = next(
            event
            for event in continuation_events
            if isinstance(event, RunFinishedEvent)
        )
        assert continuation_finish.outcome is not None
        assert continuation_finish.outcome.type == "success"
        assert any(
            isinstance(event, ToolCallResultEvent)
            and event.tool_call_id == CALL_ID
            for event in continuation_events
        )
        assert not any(
            isinstance(event, ToolCallStartEvent)
            and event.tool_call_id == CALL_ID
            for event in continuation_events
        )

    assert approved_executions == [(CALL_ID, ("a", "b", "c", "d"))]
    assert denied_executions == []
    return (
        ProbeResult(
            5,
            "AG-UI resume shape",
            "a real interrupt resumes via resume=[{interruptId, status='resolved', payload={approved: bool}}]",
        ),
        ProbeResult(
            6,
            "tool call identity",
            f"the adapter emits int-{CALL_ID}, resumes the original {CALL_ID}, and emits only its result",
        ),
    )


def run_probe() -> list[ProbeResult]:
    results = [
        probe_imports(),
        probe_static_approval_parameter(),
        probe_message_history_round_trip(),
    ]
    conditional_result, _, _ = probe_conditional_approval()
    results.append(conditional_result)
    ag_ui_result, identity_result = probe_ag_ui_interrupt_and_continuation()
    results.append(ag_ui_result)
    results.append(identity_result)
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
