from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.ui.ag_ui import AGUIAdapter


TOOL_CALL_ID = "delete-spike-item-42"
SPIKE_ITEM_ID = "demo-task-7"
executions: list[dict[str, str]] = []
requests_seen: list[dict[str, Any]] = []


def latest_request_parts(messages: list[ModelMessage]) -> list[Any]:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            return list(message.parts)
    return []


def latest_user_prompt(messages: list[ModelMessage]) -> str:
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                return part.content
    return ""


async def spike_model(
    messages: list[ModelMessage], _info: AgentInfo
) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
    tool_result = next(
        (
            part
            for part in latest_request_parts(messages)
            if isinstance(part, ToolReturnPart)
        ),
        None,
    )
    if tool_result is not None:
        if tool_result.outcome == "denied":
            yield "Decision received. The deletion was denied, so the tool body did not run."
        else:
            yield "Decision received. The deletion was approved and the tool ran exactly once."
        return

    prompt = latest_user_prompt(messages).lower()
    if "delete" in prompt:
        yield {
            0: DeltaToolCall(
                name="delete_demo_item",
                json_args=json.dumps({"item_id": SPIKE_ITEM_ID}),
                tool_call_id=TOOL_CALL_ID,
            )
        }
        return

    for chunk in (
        "AG-UI connection confirmed. ",
        "This response streamed from Pydantic AI through FastAPI ",
        "and rendered in assistant-ui.",
    ):
        yield chunk


agent = Agent(
    FunctionModel(
        stream_function=spike_model,
        model_name="trellis-t00a-function-model",
    ),
    output_type=[str, DeferredToolRequests],
)


@agent.tool_plain(requires_approval=True)
def delete_demo_item(item_id: str) -> str:
    executions.append({"tool_call_id": TOOL_CALL_ID, "item_id": item_id})
    return f"deleted {item_id}"


app = FastAPI(title="Trellis T00A disposable AG-UI spike")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type", "accept"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/spike-state")
async def spike_state() -> dict[str, Any]:
    return {
        "execution_count": len(executions),
        "executions": executions,
        "expected_item_id": SPIKE_ITEM_ID,
        "expected_tool_call_id": TOOL_CALL_ID,
        "requests_seen": requests_seen,
    }


@app.post("/reset")
async def reset() -> dict[str, Any]:
    executions.clear()
    requests_seen.clear()
    return await spike_state()


@app.post("/ag-ui")
async def ag_ui(request: Request):
    body = json.loads(await request.body())
    requests_seen.append(
        {
            "method": request.method,
            "path": request.url.path,
            "body": body,
        }
    )
    return await AGUIAdapter.dispatch_request(request, agent=agent)
