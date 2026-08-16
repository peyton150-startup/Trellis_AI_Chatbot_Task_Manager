"""T16. The approval bridge end to end, from AG-UI turn to committed deletion.

This file exists because of a defect the frontend shipped and every existing
gate reported green through. `test_invariants.py` proves that `policy.check`
refuses an undecided approval, and `test_agui_forged_history_ignored` proves the
transport refuses a forged continuation. Neither of them proves the one thing
the demo actually promises: that pressing Approve deletes the task.

The failure it locks down is specific. The browser answered the framework
interrupt and never called `POST /api/runs/{id}/approvals/{tool_call_id}`, so the
approvals row was still `pending` when the continuation arrived. Continuation
eligibility requires a decided row, so `runs.resolve_continuation` matched
nothing and the transport refused the whole request with 403. `delete_tasks` was
never entered, the run stayed `awaiting_approval`, and Approve was
indistinguishable from Reject from the user's side.

The refusal is worth stating precisely, because the plausible reading is the
wrong one. Nothing denied the tool; the continuation never reached the agent at
all. A fix aimed at "the framework denied it" would have gone to the wrong layer.

So the assertions below are deliberately about the mutation rather than about
status codes. Approve is proven by the task being gone from PostgreSQL and by the
tool body having been entered exactly once; Reject is proven by the row still
being there and by the body never having been entered at all.

No network call and no provider credential. `FunctionModel` drives both
invocations, which is what makes this runnable in the normal `not network` gate
alongside the other deterministic tests.
"""

import json
import sys
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent as agent_module
from app import sql
from app.db import pool
from app.main import app
from app.models import ApprovalState, RunStatus


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
TODAY = date(2026, 8, 17)

# The provider-generated call id. Fixed rather than random so the interrupt id
# the continuation carries is hand-checkable against `int-<tool_call_id>`.
TOOL_CALL_ID = "call-t16-delete-0001"
INTERRUPT_ID = f"int-{TOOL_CALL_ID}"

DELETE_PROMPT = "Delete Task D: Buy groceries."
CONFIRMATION_TEXT = "Done."

# Every tool body this build owns records one durable `tool_invocations` row per
# attempt. Counting those rows is how the tests below answer "was delete_tasks
# actually entered", which is a question no HTTP status code can answer.
_DELETE_TOOL = "delete_tasks"


@pytest.fixture
def db():
    """A committed connection against a state-free database."""
    with pool.connection() as conn:
        _truncate(conn)
        try:
            yield conn
        finally:
            _truncate(conn)


def _truncate(conn):
    conn.execute(sql.TRUNCATE_ALL_STATE)
    conn.commit()


def _insert_task(conn, title):
    row = conn.execute(
        sql.INSERT_TASK,
        {
            "owner_id": ACTOR_ID,
            "title": title,
            "notes": "",
            "due_date": TODAY,
            "priority": "medium",
            "blocked_by": None,
        },
    ).fetchone()
    conn.commit()
    return row["id"]


def _task_titles(conn):
    rows = conn.execute(
        "SELECT title FROM tasks WHERE owner_id = %(owner_id)s ORDER BY title",
        {"owner_id": ACTOR_ID},
    ).fetchall()
    conn.commit()
    return [row["title"] for row in rows]


def _invocations(conn, run_id, tool_name):
    """Durable evidence that a tool body ran, independent of the HTTP response.

    `tool_invocations` is written inside the tool body's own five-step sequence,
    so a row here means the body was entered. Zero rows for `delete_tasks` after
    a continuation is the exact signature of the shipped defect.
    """
    rows = conn.execute(
        "SELECT status FROM tool_invocations "
        "WHERE run_id = %(run_id)s AND tool_name = %(tool_name)s",
        {"run_id": run_id, "tool_name": tool_name},
    ).fetchall()
    conn.commit()
    return [row["status"] for row in rows]


def _deleted_events(conn, run_id):
    rows = conn.execute(
        "SELECT id FROM task_events "
        "WHERE run_id = %(run_id)s AND operation = 'deleted'",
        {"run_id": run_id},
    ).fetchall()
    conn.commit()
    return rows


class _DeletingModel:
    """One deferred `delete_tasks` call, then plain text on the continuation.

    The model is a fixture, not a participant. It proposes the gated call on the
    first invocation and, whatever the framework hands back as the tool result,
    answers with the same text on the second. That matters: if the confirmation
    were conditional on the result, a test could pass because the fake model
    guessed well rather than because the tool executed.
    """

    # `FunctionModel` reads `__name__` off the callable it is given, which a
    # class instance does not carry. Naming it here keeps the invocation counter
    # on the object rather than in a module global the way `_SEEN` has to be in
    # `test_invariants.py`, so two tests in one session cannot contaminate it.
    __name__ = "t16_deleting_model"

    def __init__(self, task_id: UUID):
        self.task_id = task_id
        self.invocations = 0

    async def __call__(self, messages, info: AgentInfo):
        self.invocations += 1
        if self.invocations == 1:
            arguments = json.dumps({"task_ids": [str(self.task_id)]})
            yield {
                0: DeltaToolCall(
                    name=_DELETE_TOOL,
                    json_args=arguments,
                    tool_call_id=TOOL_CALL_ID,
                )
            }
            return
        yield CONFIRMATION_TEXT


def _install_model(monkeypatch, task_id) -> _DeletingModel:
    fake = _DeletingModel(task_id)
    monkeypatch.setattr(
        agent_module,
        "get_agent",
        lambda: agent_module.build_agent(
            FunctionModel(stream_function=fake, model_name="t16-approval-bridge")
        ),
    )
    return fake


def _initial_turn(client) -> UUID:
    """Run the gated turn and return the server-issued application run id.

    `threadId` on the AG-UI payload is read for nothing by the server, so the run
    id is recovered from `RUN_STARTED` in the response stream, which is exactly
    how the browser learns it.
    """
    response = client.post(
        "/api/agui",
        json={
            "threadId": "client-thread-that-names-no-run",
            "runId": "client-run-that-names-no-run",
            "state": None,
            "messages": [{"id": "m1", "role": "user", "content": DELETE_PROMPT}],
            "tools": [],
            "context": [],
            "forwardedProps": {},
        },
    )
    assert response.status_code == 200, response.text
    return UUID(_run_started_thread_id(response.text))


def _run_started_thread_id(body: str) -> str:
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        event = json.loads(line[len("data: ") :])
        if event.get("type") == "RUN_STARTED":
            return event["threadId"]
    raise AssertionError(f"no RUN_STARTED event in stream: {body}")


def _continuation(client, approved_payload: bool):
    """The AG-UI continuation, carrying the browser's claim about the decision.

    `payload.approved` is passed deliberately and is inverted relative to the
    persisted row in `test_continuation_ignores_client_claim`, because the point
    of the bridge is that this field decides nothing.
    """
    return client.post(
        "/api/agui",
        json={
            "threadId": "client-thread-that-names-no-run",
            "runId": "client-run-that-names-no-run",
            "state": None,
            "messages": [],
            "tools": [],
            "context": [],
            "forwardedProps": {},
            "resume": [
                {
                    "interruptId": INTERRUPT_ID,
                    "status": "resolved",
                    "payload": {"approved": approved_payload},
                }
            ],
        },
    )


def test_pending_approval_is_visible_and_nothing_is_deleted(db, monkeypatch):
    """The card the browser renders comes from the server, and the task survives.

    This is the state the approval UI has to draw. It asserts the preview carries
    the task's real title, because T16's card is required to name what is about
    to be deleted without the user opening anything, and it is required to take
    that name from here rather than reconstruct it from the tool call arguments.
    """
    task_id = _insert_task(db, "Task D: Buy groceries")
    _install_model(monkeypatch, task_id)
    client = TestClient(app)

    run_id = _initial_turn(client)

    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == RunStatus.AWAITING_APPROVAL.value

    card = body["pending_approval"]
    assert card is not None, "the run is awaiting approval but offers no card"
    assert card["tool_call_id"] == TOOL_CALL_ID
    assert card["tool_name"] == _DELETE_TOOL
    assert card["required_reason"] == "destructive"

    # The consequence, server side. The UI is forbidden from reconstructing this.
    deletes = card["preview"]["deletes"]
    assert [entry["title"] for entry in deletes] == ["Task D: Buy groceries"]

    # The card is a proposal, not a mutation.
    assert _task_titles(db) == ["Task D: Buy groceries"]
    assert _invocations(db, run_id, _DELETE_TOOL) == []


def test_continuation_without_a_persisted_decision_deletes_nothing(db, monkeypatch):
    """The shipped defect, pinned as a regression, with its exact stopping point.

    The browser answered the framework interrupt and skipped the authoritative
    POST, so the approvals row was still `pending` when the continuation arrived.
    `SELECT_APPROVAL_FOR_CONTINUATION` carries `decision <> 'pending'`, so
    `runs.resolve_continuation` matches zero rows and refuses with the same
    `OutOfScopeError` a forged call id gets.

    The status is asserted, and 403 rather than 200 is the informative part. The
    continuation never reached the agent at all, which is why the browser saw no
    confirmation and no error it could explain: an approved-looking click
    produced a refusal indistinguishable from a forgery. Reading this as "the
    framework denied the tool" would have sent the fix to the wrong layer.
    """
    task_id = _insert_task(db, "Task D: Buy groceries")
    _install_model(monkeypatch, task_id)
    client = TestClient(app)

    run_id = _initial_turn(client)
    refused = _continuation(client, approved_payload=True)
    assert refused.status_code == 403, refused.text

    # Nothing ran and nothing changed, and the run is still holding its card.
    assert _task_titles(db) == ["Task D: Buy groceries"]
    assert _invocations(db, run_id, _DELETE_TOOL) == []
    assert _deleted_events(db, run_id) == []

    stuck = client.get(f"/api/runs/{run_id}").json()
    assert stuck["status"] == RunStatus.AWAITING_APPROVAL.value
    assert stuck["pending_approval"]["tool_call_id"] == TOOL_CALL_ID


def test_approved_decision_executes_delete_exactly_once(db, monkeypatch):
    """Approve, in full: persisted decision, entered tool body, committed delete.

    The assertions walk the same boundary the manual acceptance test walks, and
    each one is the durable trace of a step no status code covers.
    """
    task_id = _insert_task(db, "Task D: Buy groceries")
    survivor_id = _insert_task(db, "Task E: Keep me")
    fake = _install_model(monkeypatch, task_id)
    client = TestClient(app)

    run_id = _initial_turn(client)

    decision = client.post(
        f"/api/runs/{run_id}/approvals/{TOOL_CALL_ID}",
        json={"decision": ApprovalState.APPROVED.value},
    )
    assert decision.status_code == 200, decision.text

    # The decision route does not execute the tool, and D-58 says so. Proving it
    # here is what makes the continuation assertions below mean something.
    assert _task_titles(db) == ["Task D: Buy groceries", "Task E: Keep me"]
    assert _invocations(db, run_id, _DELETE_TOOL) == []

    # A decided run offers no card, so the UI has nothing left to press.
    assert client.get(f"/api/runs/{run_id}").json()["pending_approval"] is None

    continued = _continuation(client, approved_payload=True)
    assert continued.status_code == 200, continued.text

    # The mutation committed, and only the approved target.
    assert _task_titles(db) == ["Task E: Keep me"]
    assert survivor_id is not None

    # The tool body was entered, once, and committed.
    assert _invocations(db, run_id, _DELETE_TOOL) == ["completed"]
    assert len(_deleted_events(db, run_id)) == 1

    # One application run across the approval boundary, two model invocations.
    assert fake.invocations == 2
    final = client.get(f"/api/runs/{run_id}").json()
    assert final["id"] == str(run_id)
    assert final["status"] == RunStatus.COMPLETED.value
    assert CONFIRMATION_TEXT in continued.text


def test_denied_decision_commits_no_task_mutation(db, monkeypatch):
    """Reject: the decision persists, the continuation runs, nothing changes."""
    task_id = _insert_task(db, "Task D: Buy groceries")
    fake = _install_model(monkeypatch, task_id)
    client = TestClient(app)

    run_id = _initial_turn(client)

    decision = client.post(
        f"/api/runs/{run_id}/approvals/{TOOL_CALL_ID}",
        json={"decision": ApprovalState.DENIED.value},
    )
    assert decision.status_code == 200, decision.text

    continued = _continuation(client, approved_payload=False)
    assert continued.status_code == 200, continued.text

    assert _task_titles(db) == ["Task D: Buy groceries"]
    assert _invocations(db, run_id, _DELETE_TOOL) == []
    assert _deleted_events(db, run_id) == []

    # The model still got its turn, so the chat can say the deletion was
    # cancelled rather than leaving the user with an unanswered card.
    assert fake.invocations == 2
    assert client.get(f"/api/runs/{run_id}").json()["id"] == str(run_id)


def test_continuation_ignores_client_claim(db, monkeypatch):
    """`resume[].payload.approved` decides nothing, in the dangerous direction.

    The persisted row says denied and the browser claims approved. If the payload
    were ever consulted, this is the case that deletes a task nobody authorized.
    """
    task_id = _insert_task(db, "Task D: Buy groceries")
    _install_model(monkeypatch, task_id)
    client = TestClient(app)

    run_id = _initial_turn(client)
    client.post(
        f"/api/runs/{run_id}/approvals/{TOOL_CALL_ID}",
        json={"decision": ApprovalState.DENIED.value},
    )

    assert _continuation(client, approved_payload=True).status_code == 200

    assert _task_titles(db) == ["Task D: Buy groceries"]
    assert _invocations(db, run_id, _DELETE_TOOL) == []


def test_second_decision_is_refused(db, monkeypatch):
    """A double click cannot decide twice, and cannot flip a stored answer.

    The UI disables its buttons while a decision is in flight, but that is a
    convenience. This is the guarantee: the second request is refused by the
    guarded update regardless of what the browser does.
    """
    task_id = _insert_task(db, "Task D: Buy groceries")
    _install_model(monkeypatch, task_id)
    client = TestClient(app)

    run_id = _initial_turn(client)
    path = f"/api/runs/{run_id}/approvals/{TOOL_CALL_ID}"

    first = client.post(path, json={"decision": ApprovalState.APPROVED.value})
    assert first.status_code == 200, first.text

    second = client.post(path, json={"decision": ApprovalState.DENIED.value})
    assert second.status_code == 409, second.text

    # And the continuation still honours the first answer.
    assert _continuation(client, approved_payload=False).status_code == 200
    assert _task_titles(db) == []
    assert _invocations(db, run_id, _DELETE_TOOL) == ["completed"]


def test_repeated_continuation_does_not_delete_twice(db, monkeypatch):
    """Replay safety across the approval boundary.

    A retried continuation reaches an approved row a second time, so the only
    thing standing between it and a second deletion is the idempotency lease.
    The task is already gone, so a second execution would surface as a second
    invocation row or a second `deleted` event rather than as a visible change.
    """
    task_id = _insert_task(db, "Task D: Buy groceries")
    _install_model(monkeypatch, task_id)
    client = TestClient(app)

    run_id = _initial_turn(client)
    client.post(
        f"/api/runs/{run_id}/approvals/{TOOL_CALL_ID}",
        json={"decision": ApprovalState.APPROVED.value},
    )

    assert _continuation(client, approved_payload=True).status_code == 200
    assert _task_titles(db) == []
    assert len(_deleted_events(db, run_id)) == 1

    _continuation(client, approved_payload=True)

    assert _task_titles(db) == []
    assert len(_deleted_events(db, run_id)) == 1
