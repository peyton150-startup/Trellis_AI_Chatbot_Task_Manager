from __future__ import annotations

import asyncio
import json
from typing import Any

from httpx import ASGITransport, AsyncClient

from app import TOOL_CALL_ID, app


def run_input(
    *,
    run_id: str,
    messages: list[dict[str, Any]],
    approved: bool | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "threadId": "t00a-ci-thread",
        "runId": run_id,
        "tools": [],
        "context": [],
        "forwardedProps": {},
        "state": None,
        "messages": messages,
    }
    if approved is not None:
        body["resume"] = [
            {
                "interruptId": f"int-{TOOL_CALL_ID}",
                "status": "resolved",
                "payload": {"approved": approved},
            }
        ]
    return body


def delete_history() -> list[dict[str, Any]]:
    return [
        {
            "id": "delete-user-message",
            "role": "user",
            "content": "Delete the demo task",
        },
        {
            "id": "delete-assistant-message",
            "role": "assistant",
            "content": "",
            "toolCalls": [
                {
                    "id": TOOL_CALL_ID,
                    "type": "function",
                    "function": {
                        "name": "delete_demo_item",
                        "arguments": json.dumps({"item_id": "demo-task-7"}),
                    },
                }
            ],
        },
    ]


async def verify() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/reset")
        normal = await client.post(
            "/ag-ui",
            headers={"accept": "text/event-stream"},
            json=run_input(
                run_id="normal-run",
                messages=[
                    {
                        "id": "normal-user-message",
                        "role": "user",
                        "content": "Show the streaming proof",
                    }
                ],
            ),
        )
        assert normal.status_code == 200
        assert normal.text.count("TEXT_MESSAGE_CONTENT") == 3

        await client.post("/reset")
        interrupted = await client.post(
            "/ag-ui",
            headers={"accept": "text/event-stream"},
            json=run_input(run_id="interrupt-run", messages=delete_history()[:1]),
        )
        assert interrupted.status_code == 200
        assert f'"id":"int-{TOOL_CALL_ID}"' in interrupted.text
        assert f'"toolCallId":"{TOOL_CALL_ID}"' in interrupted.text
        assert (await client.get("/spike-state")).json()["execution_count"] == 0

        approved = await client.post(
            "/ag-ui",
            headers={"accept": "text/event-stream"},
            json=run_input(
                run_id="approved-run",
                messages=delete_history(),
                approved=True,
            ),
        )
        assert approved.status_code == 200
        assert "TOOL_CALL_RESULT" in approved.text
        assert (await client.get("/spike-state")).json()["execution_count"] == 1

        await client.post("/reset")
        denied = await client.post(
            "/ag-ui",
            headers={"accept": "text/event-stream"},
            json=run_input(
                run_id="denied-run",
                messages=delete_history(),
                approved=False,
            ),
        )
        assert denied.status_code == 200
        assert "TOOL_CALL_RESULT" in denied.text
        assert (await client.get("/spike-state")).json()["execution_count"] == 0

    print("PASS normal response emitted 3 streamed content events")
    print(f"PASS interrupt preserved {TOOL_CALL_ID}")
    print("PASS approval executed the tool body exactly once")
    print("PASS denial did not execute the tool body")


if __name__ == "__main__":
    asyncio.run(verify())
