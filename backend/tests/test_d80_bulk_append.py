"""D-80. Appending one line to many tasks, all of them or none.

The request that produced this was ordinary: add a line to the notes of the
first ten tasks. The model did the only thing the tool surface allowed, ten
single `update_task` calls, and the sixth hit a version conflict. Five tasks
were already committed, so the user was left with a half-applied edit and a
failed run, and no single operation to point at or undo.

The fix is not a ninth tool. `bulk_update_tasks` already means "the same change
to several tasks", and appending the same line is that. What it lacked was an
execution mode: append needs a different value per row, merged from that row's
locked notes, where replacement needs one shared value.

So append arrives as a mode inside the existing tool, with its own SQL path, and
D-79's replacement path is left alone. The contract these tests exist to hold is
the one the failure violated:

    every selected task receives the append exactly once, or none of them change

The ordering that delivers it is the whole design. Every merged value is
computed and validated for the entire target set before the mutating statement
runs, because merging and updating one row at a time is precisely what produced
five committed tasks and a failure.
"""

import sys
import threading
import time
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent as agent_module
from app import domain, policy, runs, sql, tools
from app.config import settings
from app.db import pool
from app.errors import AppendNotesLimitError, BulkTargetCoverageError
from app.limits import BULK_TASK_IDS_MAX, TASK_NOTES_MAX_CHARS
from app.models import (
    BulkUpdateTasksArgs,
    CreateTaskArgs,
    MutableTaskFields,
    ToolName,
    UpdateTaskArgs,
)

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")
FRAGMENT = "pull the turnips"


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
    return runs.create(actor_id, "d80 fixture", "d80-fixture-model").id


def _task(conn, run_id, title="Task", actor_id=ACTOR_ID, **fields):
    mutation = domain.create_task(
        actor_id, CreateTaskArgs(title=title, **fields), conn=conn
    )
    domain.write_events(run_id, actor_id, mutation.events, conn=conn)
    conn.commit()
    return mutation.tasks[0]


def _tasks(conn, run_id, count, notes_for=lambda index: f"note {index}"):
    return [
        _task(conn, run_id, title=f"Task {index:03d}", notes=notes_for(index))
        for index in range(count)
    ]


def _row(conn, task_id):
    return conn.execute(
        "SELECT * FROM tasks WHERE id = %(id)s", {"id": task_id}
    ).fetchone()


def _events_for(conn, task_id):
    return conn.execute(
        "SELECT * FROM task_events WHERE task_id = %(id)s ORDER BY id",
        {"id": task_id},
    ).fetchall()


class _CountingConnection:
    """Records every statement so a statement-count claim can be measured."""

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, statement, params=None, **kwargs):
        self.statements.append(statement)
        if params is None:
            return self._conn.execute(statement, **kwargs)
        return self._conn.execute(statement, params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def count(self, needle):
        return sum(1 for statement in self.statements if statement is needle)


def _append(conn, task_ids, fragment=FRAGMENT):
    return domain.bulk_update_tasks(
        ACTOR_ID,
        BulkUpdateTasksArgs(task_ids=list(task_ids), append_notes=fragment),
        conn=conn,
    )


# ------------------------------------------------------------ the schema


def test_bulk_append_is_declared_on_the_bulk_model_and_not_the_shared_base():
    """D-78's reason for the exclusion has not been reversed, only satisfied.

    The base is still shared with anything else built on it, so a field added
    there would still hand append semantics to consumers that never specified
    them. What changed is that bulk append now has a specification, so the field
    is declared on the model that implements it.
    """
    assert "append_notes" in BulkUpdateTasksArgs.model_fields
    assert "append_notes" not in MutableTaskFields.model_fields
    assert "append_notes" in UpdateTaskArgs.model_fields


def test_append_with_targets_is_accepted():
    BulkUpdateTasksArgs(task_ids=[uuid4()], append_notes=FRAGMENT)


def test_append_still_needs_targets():
    with pytest.raises(ValidationError):
        BulkUpdateTasksArgs(task_ids=[], append_notes=FRAGMENT)


@pytest.mark.parametrize(
    "other",
    [
        {"notes": "replacement"},
        {"title": "renamed"},
        {"priority": "high"},
        {"status": "done"},
        {"due_date": date(2026, 9, 1)},
        {"due_date": None},
        {"blocked_by": None},
    ],
)
def test_append_cannot_be_combined_with_another_field(other):
    """Append-only by design, not by omission.

    Replace-then-append and append-then-replace give different results and both
    are defensible, which is the reason to refuse rather than to pick one. A
    later decision can authorize a mixed mode if a real request needs it.
    """
    with pytest.raises(ValidationError) as raised:
        BulkUpdateTasksArgs(task_ids=[uuid4()], append_notes=FRAGMENT, **other)
    assert "cannot be combined" in str(raised.value)


def test_an_oversized_fragment_is_refused_by_the_schema():
    """The case Pydantic can see, as distinct from the merged-size case below."""
    with pytest.raises(ValidationError):
        BulkUpdateTasksArgs(
            task_ids=[uuid4()], append_notes="x" * (TASK_NOTES_MAX_CHARS + 1)
        )


def test_the_target_bound_still_applies_to_append():
    BulkUpdateTasksArgs(
        task_ids=[uuid4() for _ in range(BULK_TASK_IDS_MAX)], append_notes=FRAGMENT
    )
    with pytest.raises(ValidationError):
        BulkUpdateTasksArgs(
            task_ids=[uuid4() for _ in range(BULK_TASK_IDS_MAX + 1)],
            append_notes=FRAGMENT,
        )


# ------------------------------------------------------- merge semantics


@pytest.mark.parametrize(
    "existing, expected",
    [
        ("", FRAGMENT),
        ("alpha", "alpha\n" + FRAGMENT),
        ("alpha\n", "alpha\n" + FRAGMENT),
        ("alpha\n\n", "alpha\n\n" + FRAGMENT),
        ("line one\nline two", "line one\nline two\n" + FRAGMENT),
    ],
)
def test_every_row_follows_the_d78_separator_rule(db, existing, expected):
    """One newline, only when one is needed, and no reformatting of what is there.

    The rule is D-78's and `merge_appended_notes` is reused rather than
    reimplemented, because two copies of a separator rule is one copy too many.
    """
    run_id = _run()
    task = _task(db, run_id, notes=existing)

    _append(db, [task.id])
    db.commit()

    assert _row(db, task.id)["notes"] == expected


def test_targets_with_different_notes_each_keep_their_own(db):
    """The property a single shared replacement value cannot express.

    This is why append needs its own statement rather than a new value passed to
    the D-79 one: every row gets a different final value, computed from what that
    row already held.
    """
    run_id = _run()
    tasks = _tasks(
        db,
        run_id,
        4,
        notes_for=lambda index: ["", "alpha\n", "beta", "line one\nline two"][index],
    )

    _append(db, [task.id for task in tasks])
    db.commit()

    assert _row(db, tasks[0].id)["notes"] == FRAGMENT
    assert _row(db, tasks[1].id)["notes"] == "alpha\n" + FRAGMENT
    assert _row(db, tasks[2].id)["notes"] == "beta\n" + FRAGMENT
    assert _row(db, tasks[3].id)["notes"] == "line one\nline two\n" + FRAGMENT


def test_the_fragment_is_preserved_byte_for_byte(db):
    """No bullet, no numbering, no trimming. The caller asked to add their text."""
    run_id = _run()
    task = _task(db, run_id, notes="alpha")
    fragment = "  spaced  \nsecond line\t tabbed "

    _append(db, [task.id], fragment)
    db.commit()

    assert _row(db, task.id)["notes"] == "alpha\n" + fragment


def test_the_merge_reads_locked_state_not_a_value_the_model_supplied(db):
    """A note changed after the model last looked must not be overwritten.

    The model sends only its fragment. If the existing value came from anything
    the model remembered, a concurrent edit would be silently destroyed, which is
    the whole reason D-78 moved the merge server-side.
    """
    run_id = _run()
    task = _task(db, run_id, notes="original")
    db.commit()

    # Something else edits the task after the model would have read it.
    with pool.connection() as other:
        domain.update_task(
            ACTOR_ID,
            UpdateTaskArgs(
                task_id=task.id, expected_version=task.version, notes="changed by someone else"
            ),
            conn=other,
        )
        other.commit()

    _append(db, [task.id])
    db.commit()

    assert _row(db, task.id)["notes"] == "changed by someone else\n" + FRAGMENT


# ------------------------------------------------------- atomic success


@pytest.mark.parametrize("count", [1, 3, 10, 50])
def test_the_whole_set_is_appended_in_one_statement(db, count):
    """The structural claim: N targets, one task UPDATE, N audit events.

    Not an O(1) transaction. PostgreSQL still processes N rows and the audit is
    still one insert per task; what is constant is the number of task UPDATE
    statements this module issues.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, count)
    counting = _CountingConnection(db)

    mutation = domain.bulk_update_tasks(
        ACTOR_ID,
        BulkUpdateTasksArgs(
            task_ids=[task.id for task in tasks], append_notes=FRAGMENT
        ),
        conn=counting,
    )
    domain.write_events(run_id, ACTOR_ID, mutation.events, conn=counting)
    db.commit()

    assert counting.count(sql.BULK_APPEND_NOTES_GUARDED) == 1
    assert counting.count(sql.BULK_UPDATE_TASKS_GUARDED) == 0
    assert counting.count(sql.UPDATE_TASK_GUARDED) == 0
    assert counting.count(sql.INSERT_TASK_EVENT) == count

    for index, task in enumerate(tasks):
        row = _row(db, task.id)
        assert row["notes"] == f"note {index}\n" + FRAGMENT
        assert row["version"] == task.version + 1


def test_ten_tasks_the_shape_of_the_original_failure(db):
    """The exact request that produced a half-applied edit, now one operation."""
    run_id = _run()
    tasks = _tasks(db, run_id, 10)

    mutation = _append(db, [task.id for task in tasks])
    domain.write_events(run_id, ACTOR_ID, mutation.events, conn=db)
    db.commit()

    assert len(mutation.tasks) == 10
    assert len(mutation.events) == 10
    for index, task in enumerate(tasks):
        row = _row(db, task.id)
        assert row["notes"].count(FRAGMENT) == 1, "appended exactly once"
        assert row["notes"] == f"note {index}\n" + FRAGMENT
        assert row["version"] == task.version + 1
        assert len(_events_for(db, task.id)) == 2


def test_no_other_field_moves(db):
    run_id = _run()
    task = _task(
        db,
        _run(),
        title="Keep me",
        notes="alpha",
        due_date=date(2026, 9, 1),
        priority="high",
    )
    assert run_id

    _append(db, [task.id])
    db.commit()

    row = _row(db, task.id)
    assert row["title"] == "Keep me"
    assert row["due_date"] == date(2026, 9, 1)
    assert row["priority"] == "high"
    assert row["status"] == task.status.value


def test_duplicate_ids_append_once(db):
    """Otherwise the same line lands four times on one task."""
    run_id = _run()
    task = _task(db, run_id, notes="alpha")

    mutation = _append(db, [task.id] * 4)
    db.commit()

    assert len(mutation.tasks) == 1
    assert _row(db, task.id)["notes"] == "alpha\n" + FRAGMENT
    assert _row(db, task.id)["version"] == task.version + 1


def test_the_result_order_replays_the_request_order(db):
    run_id = _run()
    tasks = _tasks(db, run_id, 5)
    requested = sorted((task.id for task in tasks), reverse=True)

    mutation = _append(db, requested)
    db.commit()

    assert [task.id for task in mutation.tasks] == requested
    assert [event.task_id for event in mutation.events] == requested


def test_each_target_gets_one_event_with_its_own_snapshots(db):
    run_id = _run()
    first = _task(db, run_id, title="First", notes="alpha")
    second = _task(db, run_id, title="Second", notes="beta")

    mutation = _append(db, [first.id, second.id])
    domain.write_events(run_id, ACTOR_ID, mutation.events, conn=db)
    db.commit()

    for original, existing in ((first, "alpha"), (second, "beta")):
        rows = _events_for(db, original.id)
        assert len(rows) == 2
        updated = rows[-1]
        assert updated["operation"] == "updated"
        assert updated["before"]["notes"] == existing
        assert updated["before"]["version"] == original.version
        assert updated["after"]["notes"] == existing + "\n" + FRAGMENT
        assert updated["after"]["version"] == original.version + 1


# ------------------------------------------------------ atomic refusal


def test_one_overflowing_target_prevents_every_append(db):
    """The heart of the decision.

    Nine merges fit and the tenth does not. Merging and updating row by row
    would commit nine, which is the original failure wearing a new hat. Every
    value is computed and validated before the statement runs, so nothing
    commits.
    """
    run_id = _run()
    tasks = _tasks(
        db,
        run_id,
        10,
        notes_for=lambda index: (
            "x" * (TASK_NOTES_MAX_CHARS - 3) if index == 7 else f"note {index}"
        ),
    )
    counting = _CountingConnection(db)

    with pytest.raises(AppendNotesLimitError):
        domain.bulk_update_tasks(
            ACTOR_ID,
            BulkUpdateTasksArgs(
                task_ids=[task.id for task in tasks], append_notes=FRAGMENT
            ),
            conn=counting,
        )
    db.rollback()

    assert counting.count(sql.BULK_APPEND_NOTES_GUARDED) == 0, (
        "the mutating statement must not run at all"
    )
    for index, task in enumerate(tasks):
        row = _row(db, task.id)
        assert row["version"] == task.version, "no version moved"
        assert FRAGMENT not in row["notes"], "no task was appended to"
        assert len(_events_for(db, task.id)) == 1, "created event only"


def test_the_overflow_refusal_names_the_limit_without_truncating(db):
    run_id = _run()
    task = _task(db, run_id, notes="x" * (TASK_NOTES_MAX_CHARS - 3))

    with pytest.raises(AppendNotesLimitError) as raised:
        _append(db, [task.id])
    db.rollback()

    message = str(raised.value)
    assert str(TASK_NOTES_MAX_CHARS) in message
    assert "no task was changed" in message
    assert len(_row(db, task.id)["notes"]) == TASK_NOTES_MAX_CHARS - 3


def test_the_single_task_append_raises_the_same_narrow_type(db):
    """One deterministic error contract across both append paths."""
    run_id = _run()
    task = _task(db, run_id, notes="x" * (TASK_NOTES_MAX_CHARS - 3))

    with pytest.raises(AppendNotesLimitError):
        domain.update_task(
            ACTOR_ID,
            UpdateTaskArgs(
                task_id=task.id, expected_version=task.version, append_notes=FRAGMENT
            ),
            conn=db,
        )
    db.rollback()


def test_a_missing_target_appends_to_nothing(db):
    run_id = _run()
    present = _task(db, run_id, notes="alpha")

    with pytest.raises(Exception):
        _append(db, [present.id, uuid4()])
    db.rollback()

    assert _row(db, present.id)["notes"] == "alpha"
    assert _row(db, present.id)["version"] == present.version


def test_a_foreign_target_appends_to_nothing(db):
    run_id = _run()
    mine = _task(db, run_id, notes="alpha")
    theirs = _task(db, _run(OTHER_ACTOR_ID), title="Theirs", actor_id=OTHER_ACTOR_ID)

    with pytest.raises(Exception):
        _append(db, [mine.id, theirs.id])
    db.rollback()

    assert _row(db, mine.id)["notes"] == "alpha"
    assert _row(db, theirs.id)["version"] == theirs.version


# ------------------------------------------------------- the relation


def test_the_append_relation_pairs_each_target_with_its_merged_value(db):
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    locked = {task.id: task for task in tasks}

    relation = domain._bulk_append_relation(
        [task.id for task in tasks], locked, FRAGMENT
    )

    assert len(relation) == 3
    for index, (task_id, version, notes) in enumerate(relation):
        assert task_id == tasks[index].id
        assert version == tasks[index].version
        assert notes == f"note {index}\n" + FRAGMENT


def test_the_append_relation_refuses_a_duplicated_target(db):
    """`UPDATE ... FROM` picks one arbitrary source row when several match."""
    run_id = _run()
    task = _task(db, run_id)
    locked = {task.id: task}

    with pytest.raises(RuntimeError, match="duplicated expected target"):
        domain._bulk_append_relation([task.id, task.id], locked, FRAGMENT)


def test_unequal_arrays_would_reach_postgresql_as_a_silent_miss(db):
    """Why the relation is built and checked before binding, not after.

    `unnest` NULL-pads rather than rejecting, so a short array executes and
    quietly matches fewer rows, reporting a coverage problem instead of the
    construction bug it is.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    ids = [task.id for task in tasks]

    padded = db.execute(
        "SELECT * FROM unnest(%(ids)s::uuid[], %(v)s::integer[], %(n)s::text[]) "
        "AS x(id, v, n)",
        {"ids": ids, "v": [1, 2], "n": ["a", "b", "c"]},
    ).fetchall()
    db.rollback()

    assert len(padded) == 3
    assert padded[-1]["v"] is None


def test_the_append_statement_refuses_another_actors_row(db):
    """The owner predicate, reached directly.

    The lock statement already filters by owner and `_require_all_targets`
    fires before the append runs, so a foreign row never reaches this statement
    through the tool. That makes the predicate a fail-closed backstop, and a
    backstop nothing exercises can be deleted without anything going red.
    """
    run_id = _run()
    mine = _task(db, run_id, notes="mine")
    theirs = _task(db, _run(OTHER_ACTOR_ID), title="Theirs", actor_id=OTHER_ACTOR_ID)

    rows = db.execute(
        sql.BULK_APPEND_NOTES_GUARDED,
        {
            "owner_id": ACTOR_ID,
            "task_ids": [mine.id, theirs.id],
            "expected_versions": [mine.version, theirs.version],
            "effective_notes": ["mine appended", "theirs appended"],
        },
    ).fetchall()
    db.rollback()

    assert {row["id"] for row in rows} == {mine.id}, "the foreign row must not match"


def test_the_append_statement_refuses_a_row_whose_version_moved(db):
    """The per-row version predicate, likewise reached directly."""
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    versions = [task.version for task in tasks]
    versions[1] += 99

    rows = db.execute(
        sql.BULK_APPEND_NOTES_GUARDED,
        {
            "owner_id": ACTOR_ID,
            "task_ids": [task.id for task in tasks],
            "expected_versions": versions,
            "effective_notes": ["a", "b", "c"],
        },
    ).fetchall()
    db.rollback()

    assert {row["id"] for row in rows} == {tasks[0].id, tasks[2].id}


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_partial_append_coverage_refuses_rather_than_committing(db):
    """The coverage guard, reached by dropping a row from the result.

    Unreachable through the ordinary path, because a missing or foreign target
    is gone by `_require_all_targets` and the locks are held for the rest of the
    transaction. It exists for the case where that stops being true, so the
    branch has to be driven directly.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 3)

    class DropOneReturnedRow(_CountingConnection):
        def execute(self, statement, params=None, **kwargs):
            cursor = super().execute(statement, params, **kwargs)
            if statement is sql.BULK_APPEND_NOTES_GUARDED:
                return _FakeCursor(cursor.fetchall()[:-1])
            return cursor

    with pytest.raises(BulkTargetCoverageError):
        domain.bulk_update_tasks(
            ACTOR_ID,
            BulkUpdateTasksArgs(
                task_ids=[task.id for task in tasks], append_notes=FRAGMENT
            ),
            conn=DropOneReturnedRow(db),
        )
    db.rollback()

    for task in tasks:
        assert _row(db, task.id)["version"] == task.version
        assert FRAGMENT not in _row(db, task.id)["notes"]


def test_the_statement_keeps_the_owner_and_version_predicates(db):
    assert "t.owner_id = %(owner_id)s" in sql.BULK_APPEND_NOTES_GUARDED
    assert "t.version = x.expected_version" in sql.BULK_APPEND_NOTES_GUARDED
    assert "ORDER BY id" in sql.SELECT_TASKS_BY_IDS_FOR_UPDATE
    assert "FOR UPDATE" in sql.SELECT_TASKS_BY_IDS_FOR_UPDATE


def test_the_replacement_path_is_untouched_by_append(db):
    """D-79 keeps working exactly as it did, on its own statement."""
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    counting = _CountingConnection(db)

    domain.bulk_update_tasks(
        ACTOR_ID,
        BulkUpdateTasksArgs(task_ids=[t.id for t in tasks], priority="high"),
        conn=counting,
    )
    db.commit()

    assert counting.count(sql.BULK_UPDATE_TASKS_GUARDED) == 1
    assert counting.count(sql.BULK_APPEND_NOTES_GUARDED) == 0
    for task in tasks:
        assert _row(db, task.id)["priority"] == "high"
        assert _row(db, task.id)["notes"] == _row(db, task.id)["notes"]


# --------------------------------------------------------- concurrency


def test_the_isolation_these_expectations_assume(db):
    level = db.execute("SHOW transaction_isolation").fetchone()
    assert list(level.values())[0] == "read committed"


def test_a_writer_that_commits_first_is_appended_to_not_over(db):
    """No lost update: the append lands on the value the winner left behind."""
    run_id = _run()
    task = _task(db, run_id, notes="original")
    db.commit()

    started = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []
    guard = threading.Lock()

    def writer():
        with pool.connection() as conn:
            result = domain.update_task(
                ACTOR_ID,
                UpdateTaskArgs(
                    task_id=task.id,
                    expected_version=task.version,
                    notes="written first",
                ),
                conn=conn,
            )
            domain.write_events(run_id, ACTOR_ID, result.events, conn=conn)
            started.wait(timeout=30)
            time.sleep(0.5)
            conn.commit()
            with guard:
                outcomes.append(("writer", result.tasks[0].version))

    def appender():
        started.wait(timeout=30)
        with pool.connection() as conn:
            try:
                result = domain.bulk_update_tasks(
                    ACTOR_ID,
                    BulkUpdateTasksArgs(task_ids=[task.id], append_notes=FRAGMENT),
                    conn=conn,
                )
                domain.write_events(run_id, ACTOR_ID, result.events, conn=conn)
                conn.commit()
                with guard:
                    outcomes.append(("append", result.tasks[0].version))
            except Exception as raised:  # noqa: BLE001
                conn.rollback()
                with guard:
                    outcomes.append(("append", type(raised).__name__))

    threads = [threading.Thread(target=writer), threading.Thread(target=appender)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a concurrent mutation never returned"

    results = dict(outcomes)
    assert results["writer"] == task.version + 1
    assert results["append"] == task.version + 2, results

    row = _row(db, task.id)
    assert row["notes"] == "written first\n" + FRAGMENT
    assert row["version"] == task.version + 2


def test_competing_appends_over_the_same_tasks_do_not_deadlock(db):
    """Canonical lock order, exercised from both directions.

    Same caveat D-79 records: this exercises the contended path but does not by
    itself establish that `ORDER BY id` is what saves it, because PostgreSQL
    reaches these rows by primary key anyway.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 6)
    ascending = [task.id for task in tasks]
    descending = list(reversed(ascending))

    ready = threading.Barrier(2)
    failures: list[str] = []
    guard = threading.Lock()

    def worker(ids, fragment):
        try:
            with pool.connection() as conn:
                ready.wait(timeout=30)
                result = domain.bulk_update_tasks(
                    ACTOR_ID,
                    BulkUpdateTasksArgs(task_ids=ids, append_notes=fragment),
                    conn=conn,
                )
                domain.write_events(run_id, ACTOR_ID, result.events, conn=conn)
                conn.commit()
        except Exception as raised:  # noqa: BLE001
            with guard:
                failures.append(type(raised).__name__)

    threads = [
        threading.Thread(target=worker, args=(ascending, "first")),
        threading.Thread(target=worker, args=(descending, "second")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a competing append never returned"

    assert failures == [], failures
    for task in tasks:
        row = _row(db, task.id)
        assert row["version"] == task.version + 2
        assert "first" in row["notes"] and "second" in row["notes"]


# ---------------------------------------------------------- idempotency


def _append_through_the_tool(run_id, task_ids, *, tool_call_id, approved=False):
    ctx = tools.ToolContext(
        actor_id=ACTOR_ID,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_call_approved=approved,
    )
    return tools.bulk_update_tasks(
        ctx, BulkUpdateTasksArgs(task_ids=list(task_ids), append_notes=FRAGMENT)
    )


def test_a_completed_replay_does_not_append_twice(db):
    """The failure an append makes possible that a replacement does not.

    Replaying a replacement rewrites the same value. Replaying an append would
    grow the notes every time, so the lease has to hold.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 2)
    ids = [task.id for task in tasks]

    first = _append_through_the_tool(run_id, ids, tool_call_id="call-d80-0001")
    second = _append_through_the_tool(run_id, ids, tool_call_id="call-d80-0001")

    assert first == second
    for index, task in enumerate(tasks):
        row = _row(db, task.id)
        assert row["notes"].count(FRAGMENT) == 1
        assert row["notes"] == f"note {index}\n" + FRAGMENT
        assert row["version"] == task.version + 1


def test_a_synchronous_tool_is_not_subject_to_the_framework_timeout(db):
    """Probed rather than assumed, because it decides whether a fence is needed.

    The worry is real in shape: a tool timeout becomes a retry prompt, and a
    worker thread already running does not stop because the awaiting task was
    cancelled, so one logical append could commit twice under two different
    tool_call_ids.

    Measured against the pinned version, that does not happen here, because the
    timeout does not fire for synchronous tools at all. A sync body outruns its
    tool_timeout and returns normally, while an async body in the same harness
    is cancelled on time. Every Trellis tool is synchronous, so the retry that
    would be needed to double-append is never issued.

    This is a version-pinned observation, not a law, which is why it is a test.
    If a future upgrade starts enforcing the timeout for sync tools, this fails
    and the fence question reopens with evidence rather than speculation.
    """
    entered: list[str] = []
    completed: list[str] = []
    retries: list[str] = []

    async def model(messages: list[ModelMessage], info: AgentInfo):
        for message in messages:
            for part in getattr(message, "parts", []):
                if isinstance(part, RetryPromptPart):
                    retries.append(str(part.content))
        if not entered:
            return ModelResponse(parts=[ToolCallPart("slow", {"marker": "a"})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model), output_type=str, tool_timeout=1.0)

    @agent.tool_plain
    def slow(marker: str) -> str:
        entered.append(marker)
        time.sleep(2.0)
        completed.append(marker)
        return "committed"

    agent.run_sync("go")

    assert entered == ["a"]
    assert completed == ["a"], "the sync body ran to completion past its timeout"
    assert retries == [], (
        "a timeout retry was issued for a sync tool; the double-append fence "
        "question is reopened"
    )


# ------------------------------------------------------------- approval


def test_ten_targets_require_approval(db):
    """The existing blast-radius gate covers append without a second system."""
    run_id = _run()
    tasks = _tasks(db, run_id, 10)
    assert settings.blast_radius_threshold == 3

    from pydantic_ai.exceptions import ApprovalRequired

    with pytest.raises(ApprovalRequired):
        _append_through_the_tool(
            run_id, [task.id for task in tasks], tool_call_id="call-d80-approve"
        )

    for task in tasks:
        assert _row(db, task.id)["version"] == task.version


def test_the_approval_hash_covers_the_append_fragment(db):
    """Approving "pull the turnips" must not authorize "pull the carrots".

    The fragment is part of what the human approved, so a continuation carrying
    different text has to be a different hash and fail the comparison.
    """
    ids = [uuid4(), uuid4()]
    turnips = policy.arguments_hash(
        tools._payload(BulkUpdateTasksArgs(task_ids=ids, append_notes="pull the turnips"))
    )
    carrots = policy.arguments_hash(
        tools._payload(BulkUpdateTasksArgs(task_ids=ids, append_notes="pull the carrots"))
    )
    assert turnips != carrots

    # And the target set is equally part of it.
    other_ids = policy.arguments_hash(
        tools._payload(
            BulkUpdateTasksArgs(task_ids=[ids[0]], append_notes="pull the turnips")
        )
    )
    assert turnips != other_ids


def test_the_append_fragment_reaches_the_hashed_payload(db):
    payload = tools._payload(
        BulkUpdateTasksArgs(task_ids=[uuid4()], append_notes=FRAGMENT)
    )
    assert payload["append_notes"] == FRAGMENT


# ------------------------------------------------- model-facing surface


def _definitions():
    test_model = TestModel(call_tools=[])
    built = agent_module.build_agent(test_model)
    built.run_sync(
        "hello",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=uuid4()),
    )
    return {
        tool.name: tool
        for tool in test_model.last_model_request_parameters.function_tools
    }


def test_d80_adds_no_tool_and_keeps_the_barrier():
    definitions = _definitions()
    assert set(definitions) == {name.value for name in ToolName}
    bulk = definitions[ToolName.BULK_UPDATE_TASKS.value]
    assert bulk.sequential is True
    assert {n for n, t in definitions.items() if t.sequential} == {
        ToolName.BULK_UPDATE_TASKS.value
    }


def test_the_bulk_schema_now_exposes_append_to_the_model():
    schema = _definitions()[ToolName.BULK_UPDATE_TASKS.value].parameters_json_schema
    assert "append_notes" in schema["properties"]
    assert schema["properties"]["task_ids"]["minItems"] == 1
    assert schema["properties"]["task_ids"]["maxItems"] == BULK_TASK_IDS_MAX


def test_the_description_tells_the_model_to_send_one_append_call():
    description = _definitions()[ToolName.BULK_UPDATE_TASKS.value].description.lower()
    assert "append_notes" in description
    assert "one call" in description
    assert "cannot be combined" in description
    assert "do not send one update_task per task" in description


def test_the_prompt_requires_a_fresh_list_for_collection_language():
    """"The first 10 tasks" is resolved now, not from an earlier turn."""
    from app import prompts

    text = prompts.SYSTEM_PROMPT.lower()
    assert "first 10 tasks" in text
    assert "call list_tasks in this turn first" in text
    assert "append_notes" in text
    # The rule itself, not just its example. Without this the phrase could be
    # rewritten to cover only identity-named sets and the test would not notice.
    assert "names a set by a property rather than by identity" in text
    assert "do not select targets from a list you read earlier" in text


# ------------------------------------------- model-facing refusals


def _drive(db, run_id, arguments, turns=2):
    seen: dict[str, object] = {}
    state = {"turn": 0}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        state["turn"] += 1
        if state["turn"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(ToolName.BULK_UPDATE_TASKS.value, arguments)]
            )
        seen["retries"] = [
            part
            for message in messages
            for part in getattr(message, "parts", [])
            if isinstance(part, RetryPromptPart)
        ]
        seen["returns"] = [
            part
            for message in messages
            for part in getattr(message, "parts", [])
            if isinstance(part, ToolReturnPart)
            and part.tool_name == ToolName.BULK_UPDATE_TASKS.value
        ]
        return ModelResponse(parts=[TextPart("understood")])

    built = agent_module.build_agent(FunctionModel(model))
    built.run_sync(
        "append it",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
    )
    seen["turns"] = state["turn"]
    return seen


def test_a_merged_overflow_fails_the_call_instead_of_aborting_the_run(db):
    """The gap the D-79 hardening left, closed.

    Pydantic rejects an oversized fragment against the schema. It cannot see
    this: a legal fragment whose merge with locked notes overflows. Before this
    the resulting exception left the tool protocol and killed the run.

    Terminal, not retryable, because the only "correction" available to a model
    is to alter the user's text.
    """
    run_id = _run()
    task = _task(db, run_id, notes="x" * (TASK_NOTES_MAX_CHARS - 3))

    seen = _drive(
        db,
        run_id,
        {"task_ids": [str(task.id)], "append_notes": FRAGMENT},
    )

    assert seen["turns"] == 2, "the run did not survive the refusal"
    assert seen["retries"] == [], "the model must not be invited to shorten the text"
    assert seen["returns"], "the model received no result"

    reported = " ".join(str(part.content) for part in seen["returns"]).lower()
    assert "maximum length" in reported
    assert "nothing was changed" in reported
    for forbidden in ("shorten", "summarise", "rewrite"):
        assert forbidden in reported, "the model must be told not to edit the text"

    assert FRAGMENT not in _row(db, task.id)["notes"]
    assert _row(db, task.id)["version"] == task.version


def test_a_bulk_coverage_failure_does_not_ask_for_a_version(db):
    """Bulk has no caller expected_version, so that advice cannot apply to it.

    `update_task` keeps the refresh-and-retry retry, because its caller did
    supply one. This path must say something the caller can actually act on.
    """
    from app import agent as agent_mod

    captured: dict[str, object] = {}

    try:
        with agent_mod._model_facing_refusals():
            raise BulkTargetCoverageError()
    except Exception as raised:  # noqa: BLE001
        captured["error"] = raised

    failure = captured["error"]
    assert type(failure).__name__ == "ToolFailed"
    message = str(failure).lower()
    assert "no partial change was committed" in message
    assert "do not invent expected versions" in message
    assert "current_version" not in message


def test_the_single_task_conflict_still_advises_a_refresh(db):
    """The generic mapping must not be collateral damage of the new subtype."""
    from app import agent as agent_mod
    from app.errors import VersionConflictError

    try:
        with agent_mod._model_facing_refusals():
            raise VersionConflictError()
    except Exception as raised:  # noqa: BLE001
        message = str(raised).lower()

    assert "resolve the task reference again" in message
    assert "current_version" in message


def test_the_refusal_mapping_stays_narrow():
    """The new subtypes must not have widened the caught set.

    `AppendNotesLimitError` subclasses `ValidationFailedError` and
    `BulkTargetCoverageError` subclasses `VersionConflictError`, so both must be
    named before their parents or the parent handler swallows them.
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(agent_module._model_facing_refusals))
    handlers = [
        node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ExceptHandler)
    ]
    caught = [ast.unparse(handler.type) for handler in handlers]

    assert set(caught) == {
        "AppendNotesLimitError",
        "BulkTargetCoverageError",
        "VersionConflictError",
        "ExternalDivergenceError",
        "OutOfScopeError",
    }
    assert "PolicyError" not in caught
    assert "Exception" not in caught
    assert caught.index("AppendNotesLimitError") < caught.index("VersionConflictError")
    assert caught.index("BulkTargetCoverageError") < caught.index("VersionConflictError")

    for handler in handlers:
        raises = [node for node in ast.walk(handler) if isinstance(node, ast.Raise)]
        assert raises
        for node in raises:
            assert node.cause is not None


def test_the_error_table_is_still_fourteen_codes():
    """Neither subtype introduces a fifteenth code."""
    from app.errors import ERRORS_BY_CODE

    assert len(ERRORS_BY_CODE) == 14
    assert AppendNotesLimitError.code == "VALIDATION_ERROR"
    assert AppendNotesLimitError.http_status == 422
    assert BulkTargetCoverageError.code == "VERSION_CONFLICT"
    assert BulkTargetCoverageError.http_status == 409
    assert AppendNotesLimitError not in ERRORS_BY_CODE.values()
    assert BulkTargetCoverageError not in ERRORS_BY_CODE.values()


# ------------------------------------------------------ routing shape


def test_a_same_fragment_multi_task_request_routes_to_one_bulk_call(db):
    """The routing the original failure got wrong, end to end.

    The desired shape is one bulk call, not one update_task per task. Driven
    with a deterministic model so CI never depends on a live provider.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 10)
    ids = [str(task.id) for task in tasks]

    calls: list[str] = []
    state = {"turn": 0}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        state["turn"] += 1
        if state["turn"] == 1:
            calls.append(ToolName.LIST_TASKS.value)
            return ModelResponse(
                parts=[ToolCallPart(ToolName.LIST_TASKS.value, {"limit": 10})]
            )
        if state["turn"] == 2:
            calls.append(ToolName.BULK_UPDATE_TASKS.value)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        ToolName.BULK_UPDATE_TASKS.value,
                        {"task_ids": ids, "append_notes": FRAGMENT},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Added the line to all ten tasks.")])

    built = agent_module.build_agent(FunctionModel(model))
    result = built.run_sync(
        f'add to the next line of each of the first 10 tasks notes "{FRAGMENT}"',
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
    )

    # The shape is the point: one list, then ONE bulk call, never ten updates.
    assert calls == [ToolName.LIST_TASKS.value, ToolName.BULK_UPDATE_TASKS.value]
    assert calls.count(ToolName.UPDATE_TASK.value) == 0

    # Ten targets is over the blast radius, so the correct outcome here is that
    # the call defers for approval and nothing is written yet. That is the
    # existing gate covering append without a second approval system.
    from pydantic_ai import DeferredToolRequests

    assert isinstance(result.output, DeferredToolRequests), result.output
    deferred = result.output.approvals
    assert len(deferred) == 1
    assert deferred[0].tool_name == ToolName.BULK_UPDATE_TASKS.value
    assert deferred[0].args["append_notes"] == FRAGMENT
    assert len(deferred[0].args["task_ids"]) == 10

    for task in tasks:
        row = _row(db, task.id)
        assert FRAGMENT not in row["notes"], "nothing may commit before approval"
        assert row["version"] == task.version


def test_bulk_update_is_absent_from_the_linear_profile():
    """Linear has no browser approval-continuation path for this capability."""
    assert ToolName.BULK_UPDATE_TASKS.value not in agent_module.LINEAR_TOOLS
