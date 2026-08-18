"""Focused deterministic contract tests for the read-only history agent tool."""

import asyncio
from pathlib import Path
import sys
from uuid import UUID

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent as agent_module
from app import domain, sql, tools
from app.db import pool
from app.errors import IdempotencyConflictError, OutOfScopeError
from app.models import (
    CreateTaskArgs,
    DeleteTasksArgs,
    GetTaskHistoryArgs,
    UpdateTaskArgs,
)


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")
MISSING_TASK_ID = UUID("00000000-0000-0000-0000-0000000000ff")


@pytest.fixture
def db():
    """Committed PostgreSQL state, isolated from every other deterministic test."""
    with pool.connection() as conn:
        conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
        conn.commit()
        try:
            yield conn
        finally:
            conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
            conn.commit()


def _insert_run(conn, actor_id=ACTOR_ID):
    row = conn.execute(
        sql.INSERT_RUN,
        {
            "actor_id": actor_id,
            "prompt": "task history tool fixture",
            "model": "task-history-tool-test",
        },
    ).fetchone()
    conn.commit()
    return row["id"]


def _write_mutation(conn, run_id, actor_id, mutation):
    domain.write_events(
        run_id,
        actor_id,
        mutation.events,
        conn=conn,
    )
    conn.commit()
    return mutation


def _create_task(conn, actor_id, title):
    run_id = _insert_run(conn, actor_id)
    mutation = domain.create_task(
        actor_id,
        CreateTaskArgs(title=title),
        conn=conn,
    )
    return _write_mutation(conn, run_id, actor_id, mutation).tasks[0]


def _update_title(conn, actor_id, task_id, expected_version, title):
    run_id = _insert_run(conn, actor_id)
    mutation = domain.update_task(
        actor_id,
        UpdateTaskArgs(
            task_id=task_id,
            expected_version=expected_version,
            title=title,
        ),
        conn=conn,
    )
    return _write_mutation(conn, run_id, actor_id, mutation).tasks[0]


def _delete_task(conn, actor_id, task_id):
    run_id = _insert_run(conn, actor_id)
    mutation = domain.delete_tasks(
        actor_id,
        DeleteTasksArgs(task_ids=[task_id]),
        conn=conn,
    )
    return _write_mutation(conn, run_id, actor_id, mutation)


def _context(run_id, call_id):
    return tools.ToolContext(
        actor_id=ACTOR_ID,
        run_id=run_id,
        tool_call_id=call_id,
    )


def _lease(conn, run_id, call_id):
    row = conn.execute(
        """
        SELECT tool_name, arguments_hash, status, result
        FROM tool_invocations
        WHERE run_id = %(run_id)s
          AND tool_call_id = %(tool_call_id)s
        """,
        {
            "run_id": run_id,
            "tool_call_id": call_id,
        },
    ).fetchone()
    conn.commit()
    return row


def _event_count(conn):
    row = conn.execute(
        "SELECT count(*) AS n FROM task_events"
    ).fetchone()
    conn.commit()
    return row["n"]


def _approval_count(conn, run_id):
    row = conn.execute(
        """
        SELECT count(*) AS n
        FROM approvals
        WHERE run_id = %(run_id)s
        """,
        {"run_id": run_id},
    ).fetchone()
    conn.commit()
    return row["n"]


def _task_row(conn, task_id):
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = %(task_id)s",
        {"task_id": task_id},
    ).fetchone()
    conn.commit()
    return None if row is None else dict(row)


def test_history_tool_returns_authoritative_history_without_mutation(db):
    task = _create_task(db, ACTOR_ID, "History A")
    task = _update_title(
        db,
        ACTOR_ID,
        task.id,
        task.version,
        "History A edited",
    )

    run_id = _insert_run(db)
    call_id = "history-current"

    before_events = _event_count(db)
    before_task = _task_row(db, task.id)

    result = tools.get_task_history(
        _context(run_id, call_id),
        GetTaskHistoryArgs(task_id=task.id),
    )

    assert result["task_id"] == str(task.id)
    assert result["exists_now"] is True
    assert result["current_version"] == 2
    assert result["next_before_event_id"] is None

    assert len(result["entries"]) == 2

    newest, oldest = result["entries"]

    assert newest["effect"] == "updated"
    assert newest["version_before"] == 1
    assert newest["version_after"] == 2

    assert oldest["effect"] == "created"
    assert oldest["version_before"] is None
    assert oldest["version_after"] == 1

    assert _event_count(db) == before_events
    assert _task_row(db, task.id) == before_task
    assert _approval_count(db, run_id) == 0

    lease = _lease(db, run_id, call_id)

    assert lease is not None
    assert lease["tool_name"] == "get_task_history"
    assert lease["status"] == "completed"
    assert lease["result"] == result



def test_agent_history_wrapper_does_not_mark_mutation_committed(db):
    task = _create_task(db, ACTOR_ID, "Wrapper history")
    run_id = _insert_run(db)
    calls = 0

    async def model(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_task_history",
                        args={"task_id": str(task.id)},
                        tool_call_id="history-wrapper",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    deps = agent_module.TrellisDeps(
        actor_id=ACTOR_ID,
        run_id=run_id,
    )
    built = agent_module.build_agent(
        FunctionModel(model, model_name="history-wrapper")
    )

    asyncio.run(built.run("Show the task history.", deps=deps))

    assert calls == 2
    assert deps.effects.mutation_committed is False


def test_completed_history_call_replays_the_stored_page(db):
    task = _create_task(db, ACTOR_ID, "Replay A")

    run_id = _insert_run(db)
    ctx = _context(run_id, "history-replay")
    arguments = GetTaskHistoryArgs(task_id=task.id)

    first = tools.get_task_history(ctx, arguments)

    assert first["current_version"] == 1
    assert len(first["entries"]) == 1

    changed = _update_title(
        db,
        ACTOR_ID,
        task.id,
        task.version,
        "Replay A changed later",
    )

    assert changed.version == 2

    replayed = tools.get_task_history(ctx, arguments)

    assert replayed == first
    assert replayed["current_version"] == 1
    assert len(replayed["entries"]) == 1


def test_same_call_id_with_different_history_arguments_conflicts(db):
    task = _create_task(db, ACTOR_ID, "Conflict A")
    run_id = _insert_run(db)
    ctx = _context(run_id, "history-conflict")

    tools.get_task_history(
        ctx,
        GetTaskHistoryArgs(
            task_id=task.id,
            limit=20,
        ),
    )

    with pytest.raises(IdempotencyConflictError):
        tools.get_task_history(
            ctx,
            GetTaskHistoryArgs(
                task_id=task.id,
                limit=1,
            ),
        )


def test_foreign_and_missing_history_refuse_before_lease(db):
    foreign = _create_task(
        db,
        OTHER_ACTOR_ID,
        "Foreign history",
    )

    run_id = _insert_run(db)

    foreign_call = "history-foreign"
    missing_call = "history-missing"

    with pytest.raises(OutOfScopeError) as foreign_error:
        tools.get_task_history(
            _context(run_id, foreign_call),
            GetTaskHistoryArgs(task_id=foreign.id),
        )

    with pytest.raises(OutOfScopeError) as missing_error:
        tools.get_task_history(
            _context(run_id, missing_call),
            GetTaskHistoryArgs(task_id=MISSING_TASK_ID),
        )

    assert foreign_error.value.code == missing_error.value.code
    assert foreign_error.value.http_status == missing_error.value.http_status
    assert foreign_error.value.code == "OUT_OF_SCOPE"

    assert _lease(db, run_id, foreign_call) is None
    assert _lease(db, run_id, missing_call) is None


def test_deleted_task_history_is_available_by_known_id(db):
    task = _create_task(db, ACTOR_ID, "Delete history")
    _delete_task(db, ACTOR_ID, task.id)

    assert _task_row(db, task.id) is None

    run_id = _insert_run(db)

    result = tools.get_task_history(
        _context(run_id, "history-deleted"),
        GetTaskHistoryArgs(task_id=task.id),
    )

    assert result["task_id"] == str(task.id)
    assert result["exists_now"] is False
    assert result["current_version"] is None
    assert len(result["entries"]) == 2

    assert result["entries"][0]["effect"] == "deleted"
    assert result["entries"][-1]["effect"] == "created"


def test_history_tool_passes_keyset_pagination_through(db):
    task = _create_task(db, ACTOR_ID, "Page A")

    task = _update_title(
        db,
        ACTOR_ID,
        task.id,
        task.version,
        "Page B",
    )

    task = _update_title(
        db,
        ACTOR_ID,
        task.id,
        task.version,
        "Page C",
    )

    run_id = _insert_run(db)

    first = tools.get_task_history(
        _context(run_id, "history-page-1"),
        GetTaskHistoryArgs(
            task_id=task.id,
            limit=1,
        ),
    )

    assert len(first["entries"]) == 1
    assert first["next_before_event_id"] is not None

    first_event_id = first["entries"][0]["event_id"]

    second = tools.get_task_history(
        _context(run_id, "history-page-2"),
        GetTaskHistoryArgs(
            task_id=task.id,
            limit=1,
            before_event_id=first["next_before_event_id"],
        ),
    )

    assert len(second["entries"]) == 1
    assert second["entries"][0]["event_id"] < first_event_id


def test_current_task_with_no_events_returns_empty_recorded_history(db):
    row = db.execute(
        sql.INSERT_TASK,
        {
            "owner_id": ACTOR_ID,
            "title": "Seed-like history",
            "notes": "",
            "due_date": None,
            "priority": "medium",
            "blocked_by": None,
        },
    ).fetchone()
    db.commit()

    run_id = _insert_run(db)

    result = tools.get_task_history(
        _context(run_id, "history-no-events"),
        GetTaskHistoryArgs(task_id=row["id"]),
    )

    assert result["exists_now"] is True
    assert result["current_version"] == 1
    assert result["entries"] == []
    assert result["next_before_event_id"] is None
