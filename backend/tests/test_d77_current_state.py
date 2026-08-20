"""D-77. Current-state truth after deletion, and deterministic duplicate reads.

This decision exists because of an observed production failure, and the failure
was not a hallucination. `delete_tasks` returned the pre-delete snapshot, which
correctly said `"status": "open"`, because that is what the task was the instant
before it stopped existing. Pydantic AI then correctly preserved that tool return
in canonical history. A later turn showed the model a record that reads as a
currently open task with nothing anywhere saying it was gone, and the model read
it accurately.

So the fix is at the application seam, before the result reaches the framework at
all. Two markers make the postcondition explicit in the record itself, and one
bounded SQL read makes "is this a duplicate right now" a fact about current
`tasks` rows rather than about remembered snapshots.

The two halves meet in `test_undo_returns_a_task_to_its_duplicate_group`:

    delete  -> the row leaves the duplicate group
    undo    -> the same row, same id, rejoins it

which is only true if duplicate membership is computed from current state and
never from whether a task ever existed.
"""

import sys
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from psycopg.types.json import Json

from pydantic_core import to_jsonable_python
from pydantic_ai.messages import ModelMessagesTypeAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import domain, idempotency, policy, runs, sql, tools
from app.db import pool
from app.models import (
    CreateTaskArgs,
    DeleteTasksArgs,
    ListTasksArgs,
    RunStatus,
    Task,
    ToolName,
    UpdateTaskArgs,
)


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")

EMPTY_PREVIEW = {"deletes": [], "updates": []}


@pytest.fixture
def db():
    with pool.connection() as conn:
        conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
        conn.commit()
        try:
            yield conn
        finally:
            conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
            conn.commit()


# --------------------------------------------------------------- fixtures


def _run(actor_id=ACTOR_ID):
    return runs.create(actor_id, "d77 fixture", "d77-fixture-model")


def _task(conn, run_id, title, actor_id=ACTOR_ID, **fields):
    mutation = domain.create_task(
        actor_id, CreateTaskArgs(title=title, **fields), conn=conn
    )
    domain.write_events(run_id, actor_id, mutation.events, conn=conn)
    conn.commit()
    return mutation.tasks[0]


def _approve(conn, run_id, tool_call_id, task_ids):
    arguments = {"task_ids": [str(task_id) for task_id in task_ids]}
    conn.execute(
        sql.INSERT_APPROVAL,
        {
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "tool_name": ToolName.DELETE_TASKS.value,
            "arguments": Json(arguments),
            "arguments_hash": policy.arguments_hash(arguments),
            "required_reason": "destructive",
            "preview": Json(EMPTY_PREVIEW),
            "approval_ttl_seconds": 900,
        },
    )
    conn.execute(
        sql.DECIDE_APPROVAL,
        {"run_id": run_id, "tool_call_id": tool_call_id, "decision": "approved"},
    )
    conn.commit()


def _delete_through_the_tool(conn, run_id, task_ids, *, tool_call_id="call-d77-0001"):
    """One real, approved `delete_tasks` invocation. Not a domain shortcut.

    The markers are added inside the tool, before `idempotency.complete`, so a
    test that called `domain.delete_tasks` directly would prove nothing about
    the record that actually reaches history.
    """
    _approve(conn, run_id, tool_call_id, task_ids)
    ctx = tools.ToolContext(
        actor_id=ACTOR_ID,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_call_approved=True,
    )
    return tools.delete_tasks(ctx, DeleteTasksArgs(task_ids=list(task_ids)))


def _titles(results):
    return sorted(result["title"] for result in results)


def _list(conn, **kwargs):
    return domain.list_tasks(ACTOR_ID, ListTasksArgs(**kwargs), conn=conn)


# ---------------------------------------------- the delete postcondition


def test_a_successful_delete_states_its_own_postcondition(db):
    """The pre-delete snapshot survives, and stops being ambiguous about now."""
    run = _run()
    task = _task(db, run.id, "Run the farm", notes="feed the cows")

    result = _delete_through_the_tool(db, run.id, [task.id])

    assert len(result) == 1
    row = result[0]

    # The snapshot is still the pre-delete state, in full.
    assert row["id"] == str(task.id)
    assert row["title"] == "Run the farm"
    assert row["notes"] == "feed the cows"
    assert row["status"] == "open"

    # And the record now says what happened to it.
    assert row["deleted"] is True
    assert row["exists_after_tool"] is False

    assert db.execute("SELECT count(*) AS n FROM tasks").fetchone()["n"] == 0


def test_the_markers_are_the_stored_result_and_the_replayed_result(db):
    """Enrichment happens before `idempotency.complete`, and this is why.

    If the result were decorated after the lease was completed, an original call
    and a replayed one would produce two different canonical histories for the
    same invocation. The stored row is the replay, so all three must be one
    value.
    """
    run = _run()
    task = _task(db, run.id, "Fence")

    first = _delete_through_the_tool(db, run.id, [task.id])

    stored = db.execute(
        sql.SELECT_LEASE, {"run_id": run.id, "tool_call_id": "call-d77-0001"}
    ).fetchone()["result"]

    replayed = idempotency.replay_completed(
        run.id,
        "call-d77-0001",
        ToolName.DELETE_TASKS.value,
        policy.arguments_hash({"task_ids": [str(task.id)]}),
        actor_id=ACTOR_ID,
    )

    assert first == stored == replayed
    for record in (first, stored, replayed):
        assert record[0]["deleted"] is True
        assert record[0]["exists_after_tool"] is False


def test_the_markers_are_not_columns_and_not_on_the_task_model(db):
    """D-77 adds no column, no migration, and no field to `Task`.

    A row that exists never needs to say it was deleted, and a `Task` carrying
    `deleted` would have to answer that question on every ordinary read.
    """
    columns = {
        row["column_name"]
        for row in db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'tasks'"
        ).fetchall()
    }
    assert "deleted" not in columns
    assert "exists_after_tool" not in columns
    assert "deleted" not in Task.model_fields
    assert "exists_after_tool" not in Task.model_fields


def test_the_enriched_result_survives_the_canonical_history_boundary(db):
    """to_jsonable_python -> PostgreSQL jsonb -> ModelMessagesTypeAdapter.

    The whole point of the markers is that a later turn reads them, and a later
    turn reads them out of stored history rather than out of this process.
    """
    run = _run()
    task = _task(db, run.id, "Tractor")
    result = _delete_through_the_tool(db, run.id, [task.id])

    history = [
        {
            "parts": [
                {
                    "tool_name": ToolName.DELETE_TASKS.value,
                    "content": result,
                    "tool_call_id": "call-d77-0001",
                    "timestamp": "2026-08-20T00:00:00Z",
                    "part_kind": "tool-return",
                }
            ],
            "kind": "request",
        }
    ]
    runs.save_history(run.id, to_jsonable_python(history))

    reloaded = ModelMessagesTypeAdapter.validate_python(
        runs.load_history(run.id, ACTOR_ID)
    )
    returned = reloaded[0].parts[0]
    assert returned.part_kind == "tool-return"
    assert returned.content[0]["deleted"] is True
    assert returned.content[0]["exists_after_tool"] is False
    # And the historical pre-delete state is still legible beside them.
    assert returned.content[0]["status"] == "open"


# ----------------------------------------------------- duplicate reading


def test_duplicates_only_false_is_unchanged(db):
    """The default path must be byte-identical to what it always returned."""
    run = _run()
    _task(db, run.id, "Alpha")
    _task(db, run.id, "Beta")
    _task(db, run.id, "Beta")

    default = _list(db)
    explicit = _list(db, duplicates_only=False)

    assert default == explicit
    assert _titles([t.model_dump(mode="json") for t in default]) == [
        "Alpha",
        "Beta",
        "Beta",
    ]


def test_duplicates_only_returns_every_member_of_a_duplicated_title(db):
    """Members, not one representative per group."""
    run = _run()
    for title in ["Run the farm", "Run the farm", "Run the farm", "Unique"]:
        _task(db, run.id, title)

    duplicates = _list(db, duplicates_only=True)

    assert _titles([t.model_dump(mode="json") for t in duplicates]) == [
        "Run the farm"
    ] * 3


def test_duplicate_membership_is_case_insensitive_whole_title(db):
    """`lower(title)`, matching the exact arm of the D-73 resolver.

    Whole title only. A substring or similarity rule here would quietly become
    the free-text search D-73 deliberately kept behind its own bounded proof.
    """
    run = _run()
    _task(db, run.id, "Run The Farm")
    _task(db, run.id, "run the farm")
    _task(db, run.id, "Run the farm today")

    duplicates = _list(db, duplicates_only=True)

    assert _titles([t.model_dump(mode="json") for t in duplicates]) == [
        "Run The Farm",
        "run the farm",
    ]


def test_zero_rows_proves_there_are_no_duplicates(db):
    """The safe negative, and the reason stage order in the SQL matters.

    Membership is computed across the whole filtered collection before LIMIT, so
    an empty page is a real proof rather than an artefact of pagination.
    """
    run = _run()
    for n in range(60):
        _task(db, run.id, f"Distinct {n}")

    assert _list(db, duplicates_only=True) == []


def test_duplicate_membership_is_computed_before_the_page_limit(db):
    """A duplicate group must not be lost because the page was full.

    Sixty tasks form thirty pairs. If grouping ran after LIMIT, only the first
    page of rows would be considered and later pairs would vanish. Every row
    returned here is a genuine member, and the page is simply full.
    """
    run = _run()
    for n in range(30):
        _task(db, run.id, f"Pair {n}")
        _task(db, run.id, f"Pair {n}")

    duplicates = _list(db, duplicates_only=True)

    assert len(duplicates) == 50, "the page bound moved"
    counts = {}
    for task in duplicates:
        counts[task.title.lower()] = counts.get(task.title.lower(), 0) + 1
    # Every returned row belongs to a title that really is duplicated in the
    # collection, even where the page truncated the group.
    assert set(counts.values()) <= {1, 2}
    assert len(counts) >= 25


def test_filters_apply_before_duplicate_grouping(db):
    """"duplicates among open tasks", not "open tasks that share a title".

    Composing the other way would report a task as a current open duplicate
    because a done task happens to carry the same title.
    """
    run = _run()
    # `create_task` opens tasks; a done one is created and then closed, which is
    # also how a real board reaches this state.
    done = _task(db, run.id, "Harvest")
    domain.update_task(
        ACTOR_ID,
        UpdateTaskArgs(
            task_id=done.id, expected_version=done.version, status="done"
        ),
        conn=db,
    )
    db.commit()
    _task(db, run.id, "Harvest")
    _task(db, run.id, "Silo")
    _task(db, run.id, "Silo")

    open_duplicates = _list(db, duplicates_only=True, status="open")

    assert _titles([t.model_dump(mode="json") for t in open_duplicates]) == [
        "Silo",
        "Silo",
    ]

    # Unfiltered, Harvest is a duplicate group of two.
    assert len(_list(db, duplicates_only=True)) == 4


def test_a_date_filter_also_precedes_grouping(db):
    run = _run()
    _task(db, run.id, "Fence", due_date=date(2026, 1, 1))
    _task(db, run.id, "Fence", due_date=date(2026, 12, 1))

    assert _list(db, duplicates_only=True) != []
    assert _list(db, duplicates_only=True, due_before=date(2026, 6, 1)) == []


def test_duplicates_are_actor_scoped(db):
    """Another actor's identically titled task is not your duplicate."""
    mine = _run()
    theirs = _run(OTHER_ACTOR_ID)
    _task(db, mine.id, "Shared title")
    _task(db, theirs.id, "Shared title", actor_id=OTHER_ACTOR_ID)

    assert _list(db, duplicates_only=True) == []


def test_history_cannot_manufacture_a_duplicate(db):
    """The exact production failure, as a deterministic regression.

    A deleted task keeps every one of its `task_events` rows. None of them makes
    it a current duplicate, because duplicate membership reads `tasks`.
    """
    run = _run()
    first = _task(db, run.id, "Run the farm")
    _task(db, run.id, "Run the farm")

    assert len(_list(db, duplicates_only=True)) == 2

    _delete_through_the_tool(db, run.id, [first.id])

    assert _list(db, duplicates_only=True) == []

    # The durable history is still there. It simply does not count.
    events = db.execute(
        sql.SELECT_ALL_EVENTS_FOR_RUN, {"run_id": run.id}
    ).fetchall()
    assert any(event["operation"] == "deleted" for event in events)
    assert any(
        event["before"] and event["before"]["title"] == "Run the farm"
        for event in events
        if event["operation"] == "deleted"
    )


def test_the_internal_duplicate_count_never_reaches_the_task_model(db):
    """`Task` forbids extra keys, so a `marked.*` projection would fail here.

    `duplicate_count` is an internal signal in exactly the sense `match_rank` is.
    This test is what makes the explicit column list in the statement load
    bearing rather than stylistic.
    """
    run = _run()
    _task(db, run.id, "Pair")
    _task(db, run.id, "Pair")

    duplicates = _list(db, duplicates_only=True)

    assert all(isinstance(task, Task) for task in duplicates)
    for task in duplicates:
        assert "duplicate_count" not in task.model_dump(mode="json")


def test_duplicates_only_is_part_of_the_idempotency_identity(db):
    """Two different questions are two different calls, not one replayed answer.

    `duplicates_only` is inside the canonical payload, so the ordinary and the
    duplicate read hash differently and cannot collide on one lease.
    """
    ordinary = tools._payload(ListTasksArgs())
    duplicates = tools._payload(ListTasksArgs(duplicates_only=True))

    assert ordinary != duplicates
    assert policy.arguments_hash(ordinary) != policy.arguments_hash(duplicates)


def test_the_browser_profile_is_still_exactly_eight_tools():
    """D-77 adds a filter, not a ninth tool, and D-76 adds no tool at all."""
    from app import agent

    assert len(agent.ALL_TOOLS) == 8
    assert "undo" not in " ".join(agent.ALL_TOOLS)
    assert len(agent.LINEAR_TOOLS) == 6


# ------------------------------------------------------- D-76 x D-77


def test_undo_returns_a_task_to_its_duplicate_group(db):
    """The integration point of the two decisions, in one sequence.

    Duplicate truth is current PostgreSQL rows. It is not "did this task ever
    exist", and it is not "was this task deleted". Undo restores the original row
    under its original id, so the group reforms with no special handling
    anywhere.
    """
    setup = _run()
    first = _task(db, setup.id, "Run the farm")
    second = _task(db, setup.id, "Run the farm")

    assert len(_list(db, duplicates_only=True)) == 2

    delete_run = _run()
    _delete_through_the_tool(db, delete_run.id, [first.id])
    runs.set_status(delete_run.id, RunStatus.COMPLETED)

    assert _list(db, duplicates_only=True) == []

    attempt = runs.attempt_run_undo(delete_run.id, ACTOR_ID)
    assert attempt.applied == 1

    regrouped = _list(db, duplicates_only=True)
    assert {task.id for task in regrouped} == {first.id, second.id}

    # Same identity, forward version. Not a recreation.
    restored = {task.id: task for task in regrouped}[first.id]
    assert restored.created_at == first.created_at
    assert restored.version == first.version + 1


def test_deleting_the_second_member_dissolves_the_group(db):
    """Two deletions, and the group is gone rather than reduced to one."""
    setup = _run()
    tasks = [_task(db, setup.id, "Run the farm") for _ in range(3)]

    _delete_through_the_tool(
        db, _run().id, [tasks[0].id], tool_call_id="call-d77-a"
    )
    assert len(_list(db, duplicates_only=True)) == 2

    _delete_through_the_tool(
        db, _run().id, [tasks[1].id], tool_call_id="call-d77-b"
    )
    assert _list(db, duplicates_only=True) == []

    remaining = _list(db)
    assert [task.id for task in remaining] == [tasks[2].id]
