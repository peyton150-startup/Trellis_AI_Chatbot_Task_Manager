from __future__ import annotations

import asyncio
import inspect
import json
import sys
import traceback
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any, Callable

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
TOTAL_FACTS = 6


class ProbeCheckError(AssertionError):
    """Raised when a probed API fact does not hold."""


def require(condition: object, message: str) -> None:
    # Deliberately not `assert`. Python strips assert statements under
    # PYTHONOPTIMIZE, which would let this probe print its success line while
    # verifying nothing. This script's only job is to be trustworthy on the day a
    # dependency upgrade breaks a fact recorded in docs/DECISIONS.md.
    if not condition:
        raise ProbeCheckError(message)


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
    # The recorded fact is that these four names import directly from
    # `pydantic_ai`, which the module-level import above already proves. Do not
    # assert on `__module__`: the defining module is a private implementation
    # detail, and pinning it turns an internal refactor into a false failure.
    for expected_name, imported_type in expected.items():
        require(
            imported_type.__name__ == expected_name,
            f"pydantic_ai.{expected_name} resolved to {imported_type!r}",
        )
    return ProbeResult(
        1,
        "deferred approval imports",
        "top-level pydantic_ai exports: " + ", ".join(expected),
    )


def probe_static_approval_parameter() -> ProbeResult:
    parameter = inspect.signature(Agent.tool_plain).parameters.get("requires_approval")
    require(
        parameter is not None,
        "Agent.tool_plain has no requires_approval parameter",
    )
    require(
        parameter.default is False,
        f"requires_approval default is {parameter.default!r}, expected False",
    )
    require(
        parameter.annotation in {bool, "bool"},
        f"requires_approval is annotated {parameter.annotation!r}, expected bool. "
        "A widened annotation may mean conditional approval is now expressible "
        "declaratively, which would supersede API fact 4 in docs/DECISIONS.md.",
    )

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
    require(
        isinstance(first.output, DeferredToolRequests),
        f"expected a deferred request, got output {first.output!r}",
    )
    require(
        first.output.approvals[0].tool_call_id == CALL_ID,
        f"deferred approval carried tool_call_id {first.output.approvals[0].tool_call_id!r}",
    )
    require(
        executions == [],
        f"the gated tool body ran before approval: {executions!r}",
    )
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
    require(
        isinstance(json_value, list),
        f"serialized history is {type(json_value).__name__}, expected a JSON array "
        "storable in the agent_runs.message_history jsonb column",
    )
    require(
        restored == messages,
        "history did not survive the dump_json and validate_json round trip",
    )
    return ProbeResult(
        3,
        "message history round trip",
        "ModelMessagesTypeAdapter.dump_json and validate_json preserve ModelMessage values",
    )


def probe_conditional_approval() -> ProbeResult:
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
    require(
        isinstance(callable_result.output, DeferredToolRequests),
        "three ids below the threshold did not defer, so a callable may now be "
        "evaluated. That would supersede API fact 4 in docs/DECISIONS.md.",
    )
    require(
        callable_checks == [],
        f"the callable was invoked with {callable_checks!r}. A callable is no "
        "longer treated as a truthy static gate, which supersedes API fact 4 in "
        "docs/DECISIONS.md.",
    )

    below_threshold_executions: list[tuple[str, tuple[str, ...]]] = []
    below_threshold_agent = make_conditional_agent(
        below_threshold_executions, ["a", "b", "c"]
    )
    below_threshold_result = below_threshold_agent.run_sync("update three tasks")
    require(
        below_threshold_result.output == "tool completed",
        f"below-threshold run produced output {below_threshold_result.output!r}",
    )
    require(
        below_threshold_executions == [(CALL_ID, ("a", "b", "c"))],
        f"below-threshold executions were {below_threshold_executions!r}",
    )

    executions: list[tuple[str, tuple[str, ...]]] = []
    agent = make_conditional_agent(executions, ["a", "b", "c", "d"])
    first = agent.run_sync("update four tasks")
    require(
        isinstance(first.output, DeferredToolRequests),
        f"four ids over the threshold did not defer, output was {first.output!r}",
    )
    require(
        executions == [],
        f"the tool committed before approval: {executions!r}",
    )
    require(
        len(first.output.approvals) == 1,
        f"expected one deferred approval, got {len(first.output.approvals)}",
    )
    require(
        first.output.approvals[0].tool_call_id == CALL_ID,
        f"deferred approval carried tool_call_id {first.output.approvals[0].tool_call_id!r}",
    )

    history = first.all_messages()
    resumed = agent.run_sync(
        message_history=history,
        deferred_tool_results=DeferredToolResults(
            approvals={CALL_ID: ToolApproved()}
        ),
    )
    require(
        resumed.output == "tool completed",
        f"the approved continuation produced output {resumed.output!r}",
    )
    require(
        executions == [(CALL_ID, ("a", "b", "c", "d"))],
        f"approved executions were {executions!r}",
    )
    return ProbeResult(
        4,
        "conditional approval",
        "a callable is treated as a truthy static gate and is never invoked; raise ApprovalRequired in the tool",
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
        require(
            isinstance(run_input.resume, list),
            f"build_run_input parsed resume as {type(run_input.resume).__name__}",
        )
        require(
            isinstance(run_input.resume[0], ResumeEntry),
            f"resume[0] parsed as {type(run_input.resume[0]).__name__}, expected ResumeEntry",
        )
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
    require(
        len(captured_history) == 1,
        f"on_complete fired {len(captured_history)} times, expected once",
    )
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
    require(
        executions == [],
        f"the tool committed before the interrupt was answered: {executions!r}",
    )

    # The continuation sends no client messages and supplies history from the
    # server, which is the shape production uses under decision D-05.
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


def find_run_finished(events: list[Any], label: str) -> RunFinishedEvent:
    for event in events:
        if isinstance(event, RunFinishedEvent):
            return event
    raise ProbeCheckError(f"the {label} stream emitted no RUN_FINISHED event")


def probe_ag_ui_resume_shape() -> ProbeResult:
    for label, approved in (("approval", True), ("denial", False)):
        first_events, _, _ = asyncio.run(run_ag_ui_approval_flow(approved=approved))
        finish = find_run_finished(first_events, f"initial {label}")
        require(
            finish.outcome is not None,
            f"the initial {label} run finished with no outcome",
        )
        require(
            finish.outcome.type == "interrupt",
            f"the initial {label} run finished as {finish.outcome.type!r}, expected 'interrupt'",
        )
        require(
            len(finish.outcome.interrupts) == 1,
            f"the initial {label} run raised {len(finish.outcome.interrupts)} interrupts, expected one",
        )
        require(
            finish.outcome.interrupts[0].id == f"int-{CALL_ID}",
            f"interrupt id was {finish.outcome.interrupts[0].id!r}, expected 'int-{CALL_ID}'",
        )
    return ProbeResult(
        5,
        "AG-UI resume shape",
        "a real interrupt resumes via resume=[{interruptId, status='resolved', payload={approved: bool}}]",
    )


def probe_tool_call_identity() -> ProbeResult:
    flows = {
        label: asyncio.run(run_ag_ui_approval_flow(approved=approved))
        for label, approved in (("approval", True), ("denial", False))
    }

    for label, (first_events, continuation_events, _) in flows.items():
        interrupt = find_run_finished(first_events, f"initial {label}").outcome.interrupts[0]
        require(
            interrupt.tool_call_id == CALL_ID,
            f"the {label} interrupt referenced tool_call_id {interrupt.tool_call_id!r}, "
            f"expected the original {CALL_ID!r}",
        )
        require(
            any(
                isinstance(event, ToolCallStartEvent) and event.tool_call_id == CALL_ID
                for event in first_events
            ),
            f"the initial {label} stream emitted no TOOL_CALL_START for {CALL_ID}",
        )

        finish = find_run_finished(continuation_events, f"{label} continuation")
        require(
            finish.outcome is not None and finish.outcome.type == "success",
            f"the {label} continuation did not finish successfully",
        )
        require(
            any(
                isinstance(event, ToolCallResultEvent) and event.tool_call_id == CALL_ID
                for event in continuation_events
            ),
            f"the {label} continuation emitted no TOOL_CALL_RESULT for the original {CALL_ID}",
        )
        require(
            not any(
                isinstance(event, ToolCallStartEvent) and event.tool_call_id == CALL_ID
                for event in continuation_events
            ),
            f"the {label} continuation re-emitted TOOL_CALL_START, so the call was "
            "restarted rather than resumed",
        )

    approved_executions = flows["approval"][2]
    denied_executions = flows["denial"][2]
    require(
        approved_executions == [(CALL_ID, ("a", "b", "c", "d"))],
        f"approval executed the tool body as {approved_executions!r}, expected exactly "
        f"one execution under the original {CALL_ID}",
    )
    require(
        denied_executions == [],
        f"denial executed the tool body: {denied_executions!r}",
    )
    return ProbeResult(
        6,
        "tool call identity",
        f"the adapter emits int-{CALL_ID}, resumes the original {CALL_ID}, and emits only its result",
    )


CHECKS: list[tuple[int, str, Callable[[], ProbeResult]]] = [
    (1, "deferred approval imports", probe_imports),
    (2, "static approval registration", probe_static_approval_parameter),
    (3, "message history round trip", probe_message_history_round_trip),
    (4, "conditional approval", probe_conditional_approval),
    (5, "AG-UI resume shape", probe_ag_ui_resume_shape),
    (6, "tool call identity", probe_tool_call_identity),
]


def main() -> int:
    print("Trellis T00 Pydantic AI API probe")
    print(f"Python {sys.version.split()[0]}")
    print(f"pydantic-ai {version('pydantic-ai')}")
    print(f"pydantic-ai-slim {version('pydantic-ai-slim')}")
    print(f"ag-ui-protocol {version('ag-ui-protocol')}")
    print()

    confirmed: list[ProbeResult] = []
    for number, name, check in CHECKS:
        try:
            confirmed.append(check())
        except Exception as exc:
            for result in confirmed:
                print(f"PASS {result.number}/{TOTAL_FACTS} {result.name}: {result.detail}")
            print(f"FAIL {number}/{TOTAL_FACTS} {name}: {type(exc).__name__}: {exc}")
            print()
            traceback.print_exc(file=sys.stdout)
            print()
            print(
                f"Fact {number} of {TOTAL_FACTS} no longer holds for pydantic-ai "
                f"{version('pydantic-ai')} and ag-ui-protocol {version('ag-ui-protocol')}. "
                "Update the matching row in docs/DECISIONS.md and every task that "
                "consumes it before changing this probe to agree with the new behavior."
            )
            return 1

    for result in confirmed:
        print(f"PASS {result.number}/{TOTAL_FACTS} {result.name}: {result.detail}")
    print(f"ALL {TOTAL_FACTS} API FACTS CONFIRMED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
