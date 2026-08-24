"""D-79. One guarded set-based UPDATE for a whole bulk call.

`bulk_update_tasks` always presented itself to the model as one operation, and
underneath it was a loop: it locked the target set in one statement and then
issued one guarded UPDATE per task. Fifty targets meant fifty task UPDATE
statements plus fifty audit inserts, and the mutation cost grew with the size of
the set the model happened to name.

D-79 changes the statement shape and nothing else. The expectations travel to
PostgreSQL as a relation, one guarded UPDATE applies them all, and the same
owner and version predicates decide every row exactly as before. What becomes
constant is the number of task UPDATE statements this module issues, N to 1.
PostgreSQL still processes N rows, and the audit inserts stay one per task on
purpose, each carrying its own before and after snapshot. Batching those is
possible and simply is not what this decision does.

Two things bracket the SQL change. Above it, the model has to actually route a
same-patch request to this tool rather than to N single updates, and the tool
must not run interleaved with a sibling call out of the same model response.
Below it, the arrays that carry the expectations must not be able to drift,
because PostgreSQL will not tell you when they have.

The tests are ordered as the change reads: what the schema now refuses, what the
statement does, what it must not disturb, what the arrays cannot do, how it
behaves against D-78 under real concurrency, and how the model is routed to it.
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
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.models.test import TestModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent as agent_module
from app import domain, runs, sql, tools
from app.config import settings
from app.db import pool
from app.errors import VersionConflictError
from app.limits import BULK_TASK_IDS_MAX
from app.models import (
    BulkUpdateTasksArgs,
    CreateTaskArgs,
    MutableTaskFields,
    ToolName,
    UpdateTaskArgs,
)


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")


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
    return runs.create(actor_id, "d79 fixture", "d79-fixture-model").id


def _task(conn, run_id, title="Run the farm", actor_id=ACTOR_ID, **fields):
    mutation = domain.create_task(
        actor_id, CreateTaskArgs(title=title, **fields), conn=conn
    )
    domain.write_events(run_id, actor_id, mutation.events, conn=conn)
    conn.commit()
    return mutation.tasks[0]


def _tasks(conn, run_id, count, **fields):
    return [
        _task(conn, run_id, title=f"Task {index:03d}", **fields)
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
    """A connection proxy that records every statement the domain executes.

    The whole D-79 claim is about statement counts, so the count has to come
    from the real execution path rather than from reading the source. This
    forwards everything untouched and only remembers what went past.
    """

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


def _bulk(conn, task_ids, **patch):
    return domain.bulk_update_tasks(
        ACTOR_ID, BulkUpdateTasksArgs(task_ids=list(task_ids), **patch), conn=conn
    )


# ------------------------------------------------ what the schema refuses


def test_a_bulk_call_naming_no_tasks_is_refused():
    """An empty target list is a malformed call, not an empty success.

    Before D-79 this validated and returned `[]`, which the model reads as a
    completed bulk update. Nothing the user can ask for means "change these zero
    tasks".
    """
    with pytest.raises(ValidationError) as raised:
        BulkUpdateTasksArgs(task_ids=[], priority="high")
    assert "at least 1 item" in str(raised.value)


def test_the_upper_bound_on_targets_survives():
    BulkUpdateTasksArgs(
        task_ids=[uuid4() for _ in range(BULK_TASK_IDS_MAX)], priority="high"
    )
    with pytest.raises(ValidationError):
        BulkUpdateTasksArgs(
            task_ids=[uuid4() for _ in range(BULK_TASK_IDS_MAX + 1)], priority="high"
        )


def test_a_bulk_call_with_no_effective_operation_is_refused():
    """Naming targets is not enough; the patch needs an operation.

    Structural, not semantic: this refuses a patch that cannot write any column,
    never a patch whose values happen to match what is already stored. Such a
    call used to lock every target, increment every version, and write an event
    per task whose before and after are identical.
    """
    with pytest.raises(ValidationError) as raised:
        BulkUpdateTasksArgs(task_ids=[uuid4()])
    assert "at least one field to change" in str(raised.value)


def test_a_patch_matching_current_state_is_still_structurally_effective(db):
    """The rule is about the operation, not about whether the value differs.

    Setting priority to high on an already-high task is a legitimate request for
    a state, and it stays valid. A validator that tried to prove the stored value
    would differ would have to read the rows, which is a different and much
    stronger claim than the one made here.
    """
    run_id = _run()
    task = _task(db, run_id, priority="high")

    mutation = _bulk(db, [task.id], priority="high")
    db.commit()

    assert len(mutation.tasks) == 1
    assert _row(db, task.id)["priority"] == "high"
    assert _row(db, task.id)["version"] == task.version + 1


@pytest.mark.parametrize("field", ["title", "notes", "priority", "status"])
def test_an_explicit_null_on_a_coalesced_field_is_not_a_change(field):
    """These four reach SQL through COALESCE, where null means "leave it".

    So sending one explicitly is indistinguishable from omitting it, and a call
    carrying only that is still a call that changes nothing.
    """
    with pytest.raises(ValidationError):
        BulkUpdateTasksArgs(task_ids=[uuid4()], **{field: None})


@pytest.mark.parametrize(
    "patch",
    [
        {"due_date": None},
        {"blocked_by": None},
        {"notes": ""},
        {"title": "x"},
        {"priority": "high"},
        {"status": "done"},
        {"due_date": date(2026, 9, 1)},
    ],
)
def test_an_effective_patch_is_accepted(patch):
    """due_date and blocked_by carry a set flag, so their null is a real clear.

    `notes=""` is likewise a real clear, because COALESCE("", notes) is "".
    """
    BulkUpdateTasksArgs(task_ids=[uuid4()], **patch)


def test_the_replacement_path_never_carries_an_append(db):
    """D-79's statement is for replacement, and D-80 gave append its own.

    When D-79 shipped, this asserted bulk could not append at all. D-80 admitted
    it, so the surviving invariant is narrower and more useful: an append never
    travels through the replacement statement. The two are separate modes on
    separate SQL, because replacement binds one shared value and append binds a
    different merged value per row.
    """
    assert "append_notes" in BulkUpdateTasksArgs.model_fields
    assert "append_notes" not in MutableTaskFields.model_fields

    # Append is append-only, so it can never be mixed into a replacement call.
    with pytest.raises(ValidationError):
        BulkUpdateTasksArgs(task_ids=[uuid4()], append_notes="x", notes="y")

    run_id = _run()
    task = _task(db, run_id, notes="alpha")
    counting = _CountingConnection(db)
    domain.bulk_update_tasks(
        ACTOR_ID,
        BulkUpdateTasksArgs(task_ids=[task.id], append_notes="beta"),
        conn=counting,
    )
    db.commit()
    assert counting.count(sql.BULK_UPDATE_TASKS_GUARDED) == 0
    assert counting.count(sql.BULK_APPEND_NOTES_GUARDED) == 1


# ------------------------------------------------------ the statement shape


@pytest.mark.parametrize("count", [1, 3, 10, 25, 50])
def test_one_task_update_statement_regardless_of_target_count(db, count):
    """The structural claim of D-79, measured rather than asserted.

    Task UPDATEs go from N to 1. Audit inserts stay at N, deliberately, and are
    counted here so the claim cannot quietly become "all SQL is constant".
    """
    run_id = _run()
    tasks = _tasks(db, run_id, count)
    counting = _CountingConnection(db)

    mutation = domain.bulk_update_tasks(
        ACTOR_ID,
        BulkUpdateTasksArgs(task_ids=[task.id for task in tasks], priority="high"),
        conn=counting,
    )
    domain.write_events(run_id, ACTOR_ID, mutation.events, conn=counting)

    assert counting.count(sql.BULK_UPDATE_TASKS_GUARDED) == 1
    assert counting.count(sql.UPDATE_TASK_GUARDED) == 0
    assert counting.count(sql.SELECT_TASKS_BY_IDS_FOR_UPDATE) == 1
    assert counting.count(sql.INSERT_TASK_EVENT) == count
    assert len(mutation.tasks) == count
    assert len(mutation.events) == count
    db.commit()


@pytest.mark.parametrize("count", [1, 3, 10, 25, 50])
def test_every_named_task_receives_the_identical_patch(db, count):
    run_id = _run()
    tasks = _tasks(db, run_id, count)

    mutation = _bulk(
        db,
        [task.id for task in tasks],
        priority="high",
        status="done",
        notes="shared",
    )
    db.commit()

    for task in tasks:
        row = _row(db, task.id)
        assert row["priority"] == "high"
        assert row["status"] == "done"
        assert row["notes"] == "shared"
        assert row["version"] == task.version + 1
    assert {task.id for task in mutation.tasks} == {task.id for task in tasks}


def test_the_result_order_replays_the_request_order(db):
    """RETURNING has no ordering to trust, so the order is rebuilt from the id list.

    The lock statement orders by id to avoid deadlock, which is a different
    order from the request whenever the caller does not happen to pass ids
    ascending. A caller reading position rather than id would silently get the
    wrong task.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 6)
    requested = sorted((task.id for task in tasks), reverse=True)

    mutation = _bulk(db, requested, priority="low")
    db.commit()

    assert [task.id for task in mutation.tasks] == requested
    assert [event.task_id for event in mutation.events] == requested


def test_duplicate_ids_mutate_one_row_once(db):
    """D-17 keeps the raw count for approval; the physical write is deduplicated.

    One version increment, one event, not four.
    """
    run_id = _run()
    task = _task(db, run_id)

    mutation = _bulk(db, [task.id] * 4, priority="high")
    db.commit()

    assert len(mutation.tasks) == 1
    assert len(mutation.events) == 1
    assert _row(db, task.id)["version"] == task.version + 1


def test_duplicate_ids_still_count_at_full_blast_radius(db):
    """The two counts stay deliberately different, and dedup must not move earlier.

    `[A, A, A, A]` is four references for approval and one row for the write.
    D-79 deduplicates for the physical update, and the temptation is to do it
    once at the top and reuse it. That would shrink the blast radius below the
    threshold and skip an approval the policy layer requires, which is a
    security regression wearing the costume of a tidy-up.

    This runs the tool rather than the domain, because the blast-radius count
    lives in the tool body and a domain-level test cannot see it change.
    """
    run_id = _run()
    task = _task(db, run_id)
    assert settings.blast_radius_threshold == 3

    with pytest.raises(ApprovalRequired):
        _bulk_through_the_tool(
            run_id,
            [task.id] * 4,
            tool_call_id="call-d79-blast",
            priority="high",
        )

    assert _row(db, task.id)["version"] == task.version, "nothing may commit"


# ------------------------------------------------ what it must not disturb


def test_an_omitted_due_date_is_left_alone_and_an_explicit_null_clears_it(db):
    """The omitted-versus-null contract is the one most at risk in a rewrite.

    It is carried by `model_fields_set`, not by the value, and the bulk path now
    shares its parameter builder with the single-task path so the two cannot
    drift apart.
    """
    run_id = _run()
    kept, cleared = _tasks(db, run_id, 2, due_date=date(2026, 9, 1))

    _bulk(db, [kept.id], priority="high")
    db.commit()
    assert _row(db, kept.id)["due_date"] == date(2026, 9, 1)

    _bulk(db, [cleared.id], due_date=None)
    db.commit()
    assert _row(db, cleared.id)["due_date"] is None


def test_an_omitted_blocker_is_left_alone_and_an_explicit_null_clears_it(db):
    run_id = _run()
    blocker = _task(db, run_id, title="Blocker")
    kept = _task(db, run_id, title="Kept", blocked_by=blocker.id)
    cleared = _task(db, run_id, title="Cleared", blocked_by=blocker.id)

    _bulk(db, [kept.id], priority="high")
    db.commit()
    assert _row(db, kept.id)["blocked_by"] == blocker.id

    _bulk(db, [cleared.id], blocked_by=None)
    db.commit()
    assert _row(db, cleared.id)["blocked_by"] is None


def test_each_target_gets_one_event_carrying_its_own_exact_snapshots(db):
    """N tasks share one UPDATE but never share an audit row.

    before must be the row this transaction locked and after the row the
    statement returned, per task. A set-based write makes it tempting to record
    the patch once; that would make the log unable to say what each task was.
    """
    run_id = _run()
    first = _task(db, run_id, title="First", notes="alpha")
    second = _task(db, run_id, title="Second", notes="beta")

    mutation = _bulk(db, [first.id, second.id], priority="high")
    domain.write_events(run_id, ACTOR_ID, mutation.events, conn=db)
    db.commit()

    for original in (first, second):
        rows = _events_for(db, original.id)
        assert len(rows) == 2, "one created event and exactly one updated event"
        updated = rows[-1]
        assert updated["operation"] == "updated"
        assert updated["before"]["notes"] == original.notes
        assert updated["before"]["title"] == original.title
        assert updated["before"]["version"] == original.version
        assert updated["before"]["priority"] == original.priority.value
        assert updated["after"]["notes"] == original.notes
        assert updated["after"]["title"] == original.title
        assert updated["after"]["version"] == original.version + 1
        assert updated["after"]["priority"] == "high"


def test_a_missing_target_commits_nothing(db):
    run_id = _run()
    present = _task(db, run_id)
    absent = uuid4()

    with pytest.raises(VersionConflictError):
        _bulk(db, [present.id, absent], priority="high")
    db.rollback()

    assert _row(db, present.id)["version"] == present.version
    assert _row(db, present.id)["priority"] == present.priority.value


def test_a_foreign_target_commits_nothing(db):
    """Another actor's row is indistinguishable from a missing one here.

    policy.check refuses both as OUT_OF_SCOPE before the domain runs. Reached
    directly, the owner predicate means the row is simply not in the update set,
    so coverage fails and the whole call refuses.
    """
    run_id = _run()
    mine = _task(db, run_id)
    theirs = _task(db, _run(OTHER_ACTOR_ID), title="Theirs", actor_id=OTHER_ACTOR_ID)

    with pytest.raises(VersionConflictError):
        _bulk(db, [mine.id, theirs.id], priority="high")
    db.rollback()

    assert _row(db, mine.id)["version"] == mine.version
    assert _row(db, theirs.id)["version"] == theirs.version


def test_partial_coverage_refuses_rather_than_committing_what_it_got(db):
    """The guard that replaces the loop's per-statement `row is None` check.

    Through the ordinary path this is unreachable: a missing or foreign target
    is already gone by `_require_all_targets`, and the lock is held for the rest
    of the transaction, so rows do not vanish underneath it. It exists for the
    case where that stops being true, which means a test has to reach the branch
    directly. The connection proxy below drops one row from the statement's
    result, which is exactly what a row slipping out of the update set would
    look like from here.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    ids = [task.id for task in tasks]

    class DropOneReturnedRow(_CountingConnection):
        def execute(self, statement, params=None, **kwargs):
            cursor = super().execute(statement, params, **kwargs)
            if statement is sql.BULK_UPDATE_TASKS_GUARDED:
                rows = cursor.fetchall()[:-1]
                return _FakeCursor(rows)
            return cursor

    with pytest.raises(VersionConflictError):
        domain.bulk_update_tasks(
            ACTOR_ID,
            BulkUpdateTasksArgs(task_ids=ids, priority="high"),
            conn=DropOneReturnedRow(db),
        )
    db.rollback()

    for task in tasks:
        assert _row(db, task.id)["version"] == task.version
        assert _row(db, task.id)["priority"] == task.priority.value


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_the_statement_refuses_a_row_whose_version_moved(db):
    """The version predicate, reached directly rather than through the domain.

    `_require_all_targets` fires first for every case the tool can produce, so
    the predicate itself has to be exercised against the statement to be under
    test at all.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    ids = [task.id for task in tasks]
    versions = [task.version for task in tasks]
    versions[1] += 99

    rows = db.execute(
        sql.BULK_UPDATE_TASKS_GUARDED,
        domain._bulk_update_parameters(
            ACTOR_ID, ids, versions, BulkUpdateTasksArgs(task_ids=ids, priority="high")
        ),
    ).fetchall()
    db.rollback()

    returned = {row["id"] for row in rows}
    assert returned == {tasks[0].id, tasks[2].id}
    assert tasks[1].id not in returned


def test_the_statement_refuses_another_actors_row(db):
    """The owner predicate, likewise reached directly.

    The lock statement already filters by owner, so a foreign row never reaches
    this statement through `bulk_update_tasks`. That makes the predicate here a
    fail-closed backstop rather than the primary check, and a backstop nothing
    exercises is a backstop that can be deleted without anything going red.
    """
    run_id = _run()
    mine = _task(db, run_id)
    theirs = _task(db, _run(OTHER_ACTOR_ID), title="Theirs", actor_id=OTHER_ACTOR_ID)
    ids = [mine.id, theirs.id]
    versions = [mine.version, theirs.version]

    rows = db.execute(
        sql.BULK_UPDATE_TASKS_GUARDED,
        domain._bulk_update_parameters(
            ACTOR_ID, ids, versions, BulkUpdateTasksArgs(task_ids=ids, priority="high")
        ),
    ).fetchall()
    db.rollback()

    assert {row["id"] for row in rows} == {mine.id}, "the foreign row must not match"


# ------------------------------------------------------- the array contract


def test_unequal_arrays_are_refused_before_they_reach_postgresql(db):
    """PostgreSQL would accept them, and that is the problem.

    unnest over two arrays NULL-pads the shorter one, and `version = NULL`
    matches no row, so a dropped expected version fails closed but arrives
    dressed as a target-coverage conflict. This proves the padding is real and
    that the domain refuses the malformed relation before the statement runs.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    ids = [task.id for task in tasks]

    padded = db.execute(
        "SELECT * FROM unnest(%(ids)s::uuid[], %(versions)s::integer[]) AS x(id, v)",
        {"ids": ids, "versions": [1, 2]},
    ).fetchall()
    assert len(padded) == 3
    assert padded[-1]["v"] is None, "the shorter array is padded, not rejected"

    rows = db.execute(
        sql.BULK_UPDATE_TASKS_GUARDED,
        {
            **domain._bulk_update_parameters(
                ACTOR_ID, ids, [t.version for t in tasks],
                BulkUpdateTasksArgs(task_ids=ids, priority="high"),
            ),
            "expected_versions": [tasks[0].version, tasks[1].version],
        },
    ).fetchall()
    db.rollback()
    assert len(rows) == 2, "the dropped expectation silently becomes a miss"


def test_the_expected_relation_pairs_every_target_with_its_locked_version(db):
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    locked = {task.id: task for task in tasks}
    ids = [task.id for task in tasks]

    update_ids, expected_versions = domain._expected_relation(ids, locked)

    assert update_ids == ids
    assert expected_versions == [task.version for task in tasks]
    assert len(update_ids) == len(expected_versions)


def test_the_relation_builder_refuses_a_duplicated_target(db):
    """The UPDATE ... FROM hazard, refused where it can still be named.

    Two source rows for one target row means PostgreSQL applies one of them and
    discards the other, with no error. `_unique_ids` prevents it upstream; this
    is what catches a future caller that forgets to.
    """
    run_id = _run()
    task = _task(db, run_id)
    locked = {task.id: task}

    with pytest.raises(RuntimeError, match="duplicated expected target"):
        domain._expected_relation([task.id, task.id], locked)


def test_the_relation_builder_refuses_a_relation_of_the_wrong_size(db):
    """A malformed relation must not reach SQL and be misreported there.

    unnest would NULL-pad it and the missing expectation would come back as a
    coverage conflict, which points at concurrency rather than at the bug.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 2)
    locked = {task.id: task for task in tasks}

    class ShortList(list):
        def __len__(self):
            return super().__len__() + 1

    with pytest.raises(RuntimeError, match="wrong size"):
        domain._expected_relation(ShortList([task.id for task in tasks]), locked)


def test_the_relation_builder_refuses_a_null_expected_version(db):
    run_id = _run()
    task = _task(db, run_id)
    broken = task.model_copy(update={"version": None})

    with pytest.raises(RuntimeError, match="null expected version"):
        domain._expected_relation([task.id], {task.id: broken})


def test_the_sql_binder_refuses_unequal_arrays_whoever_built_them(db):
    """The last boundary before the values become SQL parameters.

    `_expected_relation` validates its own construction, but the two arrays are
    separate values the moment it returns them, and `_bulk_update_parameters`
    takes them as independent arguments. So a drop that happens after
    construction, or a future second caller that assembles the arrays itself,
    would sail past the earlier checks. `unnest` NULL-pads rather than rejects,
    so that reaches PostgreSQL, fails closed, and reports a coverage conflict
    instead of the construction bug it is.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    locked = {task.id: task for task in tasks}
    ids = [task.id for task in tasks]

    update_ids, expected_versions = domain._expected_relation(ids, locked)
    assert len(update_ids) == len(expected_versions) == 3

    with pytest.raises(RuntimeError, match="length mismatch"):
        domain._bulk_update_parameters(
            ACTOR_ID,
            update_ids,
            expected_versions[:-1],
            BulkUpdateTasksArgs(task_ids=ids, priority="high"),
        )

    with pytest.raises(RuntimeError, match="length mismatch"):
        domain._bulk_update_parameters(
            ACTOR_ID,
            update_ids[:-1],
            expected_versions,
            BulkUpdateTasksArgs(task_ids=ids, priority="high"),
        )


def test_the_lock_keeps_its_canonical_order(db):
    """A set-based UPDATE does not remove the need for the ordered lock.

    Two callers naming the same tasks in opposite orders would otherwise lock
    A then B and B then A, which is a cycle PostgreSQL breaks by aborting one
    of them.
    """
    assert "ORDER BY id" in sql.SELECT_TASKS_BY_IDS_FOR_UPDATE
    assert "FOR UPDATE" in sql.SELECT_TASKS_BY_IDS_FOR_UPDATE
    assert "owner_id = %(owner_id)s" in sql.BULK_UPDATE_TASKS_GUARDED
    assert "t.version = x.expected_version" in sql.BULK_UPDATE_TASKS_GUARDED


def test_competing_bulk_calls_over_the_same_tasks_do_not_deadlock(db):
    """Two callers naming the same tasks in opposite orders both complete.

    Be exact about what this establishes. It exercises the contended path and
    would catch a change that made competing bulk calls abort. It does NOT
    establish that `ORDER BY id` is what saves them: deleting that clause from
    `SELECT_TASKS_BY_IDS_FOR_UPDATE` leaves this test passing, repeatedly,
    because PostgreSQL reaches these rows by primary key and hands them back in
    id order anyway. The clause is doing real work only once a plan exists that
    would return them in some other order, and this fixture does not produce
    one.

    So the canonical order is pinned by the structural assertion in
    `test_the_lock_keeps_its_canonical_order`, and that limitation is recorded
    rather than hidden behind a behavioural test that cannot see it. Forcing the
    planner off the index would close the gap and was considered; it was not
    adopted because it would make the regression test depend on planner settings
    the application never sets.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 6)
    ascending = [task.id for task in tasks]
    descending = list(reversed(ascending))

    ready = threading.Barrier(2)
    failures: list[str] = []
    guard = threading.Lock()

    def worker(ids, priority):
        try:
            with pool.connection() as conn:
                ready.wait(timeout=30)
                domain.bulk_update_tasks(
                    ACTOR_ID,
                    BulkUpdateTasksArgs(task_ids=ids, priority=priority),
                    conn=conn,
                )
                conn.commit()
        except Exception as raised:  # noqa: BLE001
            with guard:
                failures.append(type(raised).__name__)

    threads = [
        threading.Thread(target=worker, args=(ascending, "high")),
        threading.Thread(target=worker, args=(descending, "low")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a competing bulk update never returned"

    assert failures == [], failures
    for task in tasks:
        assert _row(db, task.id)["version"] == task.version + 2


# ------------------------------------------------------------ idempotency


def _bulk_through_the_tool(run_id, task_ids, *, tool_call_id, **patch):
    ctx = tools.ToolContext(
        actor_id=ACTOR_ID,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_call_approved=False,
    )
    return tools.bulk_update_tasks(
        ctx, BulkUpdateTasksArgs(task_ids=list(task_ids), **patch)
    )


def test_a_completed_replay_does_not_mutate_a_second_time(db):
    """One version increment across two identical calls, not two."""
    run_id = _run()
    tasks = _tasks(db, run_id, 2)
    ids = [task.id for task in tasks]

    first = _bulk_through_the_tool(
        run_id, ids, tool_call_id="call-d79-0001", priority="high"
    )
    second = _bulk_through_the_tool(
        run_id, ids, tool_call_id="call-d79-0001", priority="high"
    )

    assert first == second
    for task in tasks:
        assert _row(db, task.id)["version"] == task.version + 1
        assert len(_events_for(db, task.id)) == 2


# ------------------------------------------------- D-78 against D-79


def test_the_probed_isolation_is_the_one_these_expectations_assume(db):
    """The concurrency cases below are only true at READ COMMITTED.

    At REPEATABLE READ or SERIALIZABLE a locked row that changed since the
    snapshot can raise instead of being handed over, so case B would not hold.
    The pool sets no isolation level, which means the server default decides,
    and a server default can be changed outside this repository. So it is
    asserted rather than assumed.
    """
    level = db.execute("SHOW transaction_isolation").fetchone()
    assert list(level.values())[0] == "read committed"


def test_a_bulk_update_that_wins_leaves_a_stale_append_refused(db):
    """Case A of two commit orderings, under the isolation asserted above.

    This exercises the ordering with real transactions on two connections. It
    does not observe the lock-wait state at a particular instant, and does not
    claim to: the barrier plus the held transaction make the ordering reliable,
    and the assertions are about the committed outcome.

    The append carries a caller-supplied expected_version and loses.

    The patch deliberately does not touch notes: a bulk call that replaced notes
    could legitimately overwrite an append, and then the test would pass for a
    reason that has nothing to do with the guard.
    """
    run_id = _run()
    task = _task(db, run_id, notes="alpha")
    db.commit()

    started = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []
    guard = threading.Lock()

    def bulk_first():
        with pool.connection() as conn:
            result = domain.bulk_update_tasks(
                ACTOR_ID,
                BulkUpdateTasksArgs(task_ids=[task.id], priority="high"),
                conn=conn,
            )
            domain.write_events(run_id, ACTOR_ID, result.events, conn=conn)
            started.wait(timeout=30)
            time.sleep(0.5)
            conn.commit()
            with guard:
                outcomes.append(("bulk", result.tasks[0].version))

    def append_second():
        started.wait(timeout=30)
        with pool.connection() as conn:
            try:
                domain.update_task(
                    ACTOR_ID,
                    UpdateTaskArgs(
                        task_id=task.id,
                        expected_version=task.version,
                        append_notes="beta",
                    ),
                    conn=conn,
                )
                with guard:
                    outcomes.append(("append", "no error"))
            except Exception as raised:  # noqa: BLE001
                conn.rollback()
                with guard:
                    outcomes.append(("append", type(raised).__name__))

    threads = [
        threading.Thread(target=bulk_first),
        threading.Thread(target=append_second),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a concurrent mutation never returned"

    results = dict(outcomes)
    assert results["bulk"] == task.version + 1
    assert results["append"] == "VersionConflictError", results

    row = _row(db, task.id)
    assert row["notes"] == "alpha", "the refused append wrote nothing"
    assert row["priority"] == "high"
    assert row["version"] == task.version + 1
    assert len(_events_for(db, task.id)) == 2, "created plus the bulk update only"


def test_an_append_that_wins_survives_the_bulk_update_behind_it(db):
    """Case B, the opposite ordering, same caveat about what it observes.

    The bulk call has no caller expected_version, so it does not lose.

    It reads each version from the row it locked, and at READ COMMITTED a waiter
    is handed the row as the winner left it. So the append commits, the bulk
    update then applies on top of the appended value, and both survive.
    """
    run_id = _run()
    task = _task(db, run_id, notes="alpha")
    db.commit()

    started = threading.Barrier(2)
    outcomes: list[tuple[str, object]] = []
    guard = threading.Lock()

    def append_first():
        with pool.connection() as conn:
            result = domain.update_task(
                ACTOR_ID,
                UpdateTaskArgs(
                    task_id=task.id,
                    expected_version=task.version,
                    append_notes="beta",
                ),
                conn=conn,
            )
            domain.write_events(run_id, ACTOR_ID, result.events, conn=conn)
            started.wait(timeout=30)
            time.sleep(0.5)
            conn.commit()
            with guard:
                outcomes.append(("append", result.tasks[0].version))

    def bulk_second():
        started.wait(timeout=30)
        with pool.connection() as conn:
            try:
                result = domain.bulk_update_tasks(
                    ACTOR_ID,
                    BulkUpdateTasksArgs(task_ids=[task.id], priority="high"),
                    conn=conn,
                )
                domain.write_events(run_id, ACTOR_ID, result.events, conn=conn)
                conn.commit()
                with guard:
                    outcomes.append(("bulk", result.tasks[0].version))
            except Exception as raised:  # noqa: BLE001
                conn.rollback()
                with guard:
                    outcomes.append(("bulk", type(raised).__name__))

    threads = [
        threading.Thread(target=append_first),
        threading.Thread(target=bulk_second),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a concurrent mutation never returned"

    results = dict(outcomes)
    assert results["append"] == task.version + 1
    assert results["bulk"] == task.version + 2, results

    row = _row(db, task.id)
    assert row["notes"] == "alpha\nbeta", "the append survived the bulk update"
    assert row["priority"] == "high"
    assert row["version"] == task.version + 2
    assert len(_events_for(db, task.id)) == 3


# ------------------------------------------------------- model-facing routing


def _definitions():
    """The tool definitions exactly as the model is shown them.

    Same route the D-78 schema tests use: drive one turn with `TestModel` and
    read what was actually sent, rather than introspecting the agent's internals.
    """
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


def test_the_bulk_tool_is_registered_as_a_sequential_barrier():
    """Metadata half of the barrier claim.

    `max_concurrency=1` bounds concurrent agent runs and says nothing about two
    tool calls arriving in one model response, which Pydantic AI schedules
    concurrently by default.
    """
    definition = _definitions()[ToolName.BULK_UPDATE_TASKS.value]
    assert definition.sequential is True


def test_only_the_bulk_tool_is_a_barrier():
    """The scope is one tool, not the whole run turned sequential."""
    sequential = {
        name for name, tool in _definitions().items() if tool.sequential
    }
    assert sequential == {ToolName.BULK_UPDATE_TASKS.value}


def test_d79_adds_no_new_model_facing_tool():
    """The tool count is the same before and after; only its wiring changed."""
    definitions = _definitions()
    assert set(definitions) == {name.value for name in ToolName}
    assert ToolName.BULK_UPDATE_TASKS.value in definitions


def test_the_bulk_description_tells_the_model_when_to_prefer_one_call():
    """The routing fix is a description the model can act on, not a new tool.

    Without an explicit preference, a same-patch request over three tasks reads
    just as naturally as three update_task calls.
    """
    description = _definitions()[ToolName.BULK_UPDATE_TASKS.value].description
    lowered = description.lower()
    assert "same" in lowered
    assert "one" in lowered
    assert "update_task" in lowered
    assert "append" in lowered, "bulk cannot append, and the model must know"


def test_the_bulk_description_carries_the_rules_the_schema_cannot():
    """The cross-field rule is invisible in JSON Schema, so it must be in words.

    `minItems` reaches the model through the generated schema. The
    structurally-effective-operation rule cannot: it is a validator that runs
    after parsing. If the description stops stating it, the model has no way to
    learn it except by failing, and the only thing still protecting it is a
    retry loop. Asserting the schema alone would not notice that.
    """
    description = _definitions()[ToolName.BULK_UPDATE_TASKS.value].description
    lowered = description.lower()

    assert "at least one field" in lowered, (
        "the description no longer tells the model a patch needs an operation"
    )
    assert "omit" in lowered, "the description no longer says to omit unchanged fields"
    assert "null" in lowered, "the description no longer explains what null means"
    assert "due_date" in lowered and "blocked_by" in lowered, (
        "the description no longer names the two fields null can legitimately clear"
    )


def test_the_bulk_schema_the_model_sees_carries_the_bound_and_no_append():
    schema = _definitions()[ToolName.BULK_UPDATE_TASKS.value].parameters_json_schema
    assert schema["properties"]["task_ids"]["maxItems"] == BULK_TASK_IDS_MAX
    assert schema["properties"]["task_ids"]["minItems"] == 1
    # D-80 added append to this schema deliberately; the bounds are what this
    # test exists to hold.
    assert "append_notes" in schema["properties"]


def test_a_sequential_tool_does_not_overlap_a_sibling_in_the_same_response():
    """Pydantic AI framework behaviour, proven by synchronisation rather than timing.

    This is a claim about the framework, not about Trellis registration: it
    establishes what `sequential=True` buys, using a throwaway agent. That the
    real bulk tool carries the flag is a separate proof, above.

    The barrier tool asks, with a bounded wait, whether its sibling has started.
    Under the barrier the sibling cannot have started, so the wait times out and
    the answer is "isolated". Without the barrier the sibling is scheduled
    alongside it on a worker thread, sets the event, and the wait returns early
    with "overlap". Nothing here infers concurrency from elapsed time, so there
    is no race to lose and no sleep to tune: the only timeout is the bound that
    stops a regression from hanging the suite.
    """
    sibling_started = threading.Event()
    observed: list[str] = []
    guard = threading.Lock()

    def note(event: str):
        with guard:
            observed.append(event)

    agent = Agent(FunctionModel(_two_calls_then_stop()), output_type=str)

    @agent.tool_plain(sequential=True)
    def bulk_like(marker: str) -> str:
        note(f"bulk-enter:{marker}")
        overlapped = sibling_started.wait(timeout=1)
        note("overlap" if overlapped else "isolated")
        return "ok"

    @agent.tool_plain
    def sibling(marker: str) -> str:
        sibling_started.set()
        note(f"sibling:{marker}")
        return "ok"

    agent.run_sync("go")

    assert "isolated" in observed, f"the sibling ran inside the barrier: {observed}"
    assert "overlap" not in observed, observed
    assert observed.index("bulk-enter:a") < observed.index("sibling:b"), observed


def _two_calls_then_stop():
    """One response carrying two tool calls, then a plain reply."""
    state = {"called": False}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        if state["called"]:
            return ModelResponse(parts=[TextPart("done")])
        state["called"] = True
        return ModelResponse(
            parts=[
                ToolCallPart("bulk_like", {"marker": "a"}),
                ToolCallPart("sibling", {"marker": "b"}),
            ]
        )

    return model


def test_the_prompt_routes_a_same_patch_request_to_one_bulk_call():
    """The system prompt has to carry the rule too, not only the tool docstring."""
    from app import prompts

    text = prompts.SYSTEM_PROMPT.lower()
    assert "bulk_update_tasks" in text
    assert "same change" in text or "same patch" in text
    assert "update_task call per task" in text or "single calls" in text


def _retry_parts(messages):
    """Every RetryPromptPart Pydantic AI has sent back to the model so far."""
    return [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, RetryPromptPart)
    ]


@pytest.mark.parametrize(
    "invalid, expected_signal",
    [
        ({"task_ids": [], "priority": "high"}, "at least 1 item"),
        ({"task_ids": ["<id>"]}, "at least one field to change"),
        ({"task_ids": ["<id>"], "priority": None}, "at least one field to change"),
    ],
)
def test_an_invalid_bulk_call_is_retried_before_it_can_mutate(
    db, monkeypatch, invalid, expected_signal
):
    """The model-boundary half of D-79's two new refusals.

    The `task_ids` bound is visible in the JSON schema, but the
    structurally-effective-operation rule is a cross-field validator that runs
    after parsing, so the model cannot read it off the schema and will
    sometimes emit a call that breaks it. What has to be true then is the
    lifecycle Pydantic AI documents: arguments are validated before the tool
    runs, a `ValidationError` becomes a `RetryPromptPart` carrying the details,
    and the tool body never executes.

    Proving "the schema says minItems 1" does not prove any of that. This drives
    a real invalid call through the real agent seam and watches where it stops.
    """
    run_id = _run()
    task = _task(db, run_id)

    arguments = dict(invalid)
    if arguments["task_ids"] == ["<id>"]:
        arguments["task_ids"] = [str(task.id)]

    entered: list[str] = []
    real_tool = tools.bulk_update_tasks

    def spy(ctx, args):
        entered.append("body")
        return real_tool(ctx, args)

    monkeypatch.setattr(tools, "bulk_update_tasks", spy)

    seen: dict[str, object] = {}
    state = {"turn": 0}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        state["turn"] += 1
        if state["turn"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(ToolName.BULK_UPDATE_TASKS.value, arguments)]
            )
        if state["turn"] == 2:
            # The invalid call must have come back as a retry, and it must not
            # have reached the tool body on its way.
            seen["retries"] = _retry_parts(messages)
            seen["entered_before_retry"] = list(entered)
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        ToolName.BULK_UPDATE_TASKS.value,
                        {"task_ids": [str(task.id)], "priority": "high"},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    built = agent_module.build_agent(FunctionModel(model))
    built.run_sync(
        "set them to high priority",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
    )

    retries = seen["retries"]
    assert retries, "the invalid call produced no retry prompt"
    assert any(
        part.tool_name == ToolName.BULK_UPDATE_TASKS.value for part in retries
    ), retries
    detail = " ".join(str(part.content) for part in retries)
    assert expected_signal in detail, detail

    assert seen["entered_before_retry"] == [], (
        "the invalid call reached the tool body before validation refused it"
    )

    # The corrected call went through the ordinary path exactly once.
    assert entered == ["body"], entered
    row = _row(db, task.id)
    assert row["priority"] == "high"
    assert row["version"] == task.version + 1
    assert len(_events_for(db, task.id)) == 2


@pytest.mark.parametrize("field", ["due_date", "blocked_by"])
def test_an_explicit_null_clear_is_not_treated_as_an_invalid_call(db, field):
    """The refusal must not catch the two fields whose null is a real value.

    Guards the obvious overcorrection: a validator that rejected every null
    would also reject the only way to clear a due date or a blocker in bulk.
    """
    run_id = _run()
    blocker = _task(db, run_id, title="Blocker")
    task = _task(
        db,
        run_id,
        title="Target",
        due_date=date(2026, 9, 1),
        blocked_by=blocker.id,
    )

    entered: list[str] = []
    real_tool = tools.bulk_update_tasks

    async def model(messages: list[ModelMessage], info: AgentInfo):
        if entered:
            return ModelResponse(parts=[TextPart("done")])
        entered.append("called")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    ToolName.BULK_UPDATE_TASKS.value,
                    {"task_ids": [str(task.id)], field: None},
                )
            ]
        )

    built = agent_module.build_agent(FunctionModel(model))
    built.run_sync(
        f"clear the {field}",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
    )
    assert real_tool is tools.bulk_update_tasks

    row = _row(db, task.id)
    assert row[field] is None, f"{field} was not cleared"
    assert row["version"] == task.version + 1


def test_a_model_emitted_bulk_call_reaches_the_database(db):
    """End to end: the model asks once, and every named task changes once.

    The two validators D-79 added sit between the model and the tool body, so
    this is where a bound that no real call can satisfy would show up. It also
    proves the routing this decision asks for is actually executable: one call,
    several tasks, one shared patch.
    """
    run_id = _run()
    tasks = _tasks(db, run_id, 3)
    ids = [str(task.id) for task in tasks]

    state = {"called": False}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        if state["called"]:
            return ModelResponse(parts=[TextPart("done")])
        state["called"] = True
        return ModelResponse(
            parts=[
                ToolCallPart(
                    ToolName.BULK_UPDATE_TASKS.value,
                    {"task_ids": ids, "priority": "high"},
                )
            ]
        )

    built = agent_module.build_agent(FunctionModel(model))
    built.run_sync(
        "set them all to high priority",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
    )

    for task in tasks:
        row = _row(db, task.id)
        assert row["priority"] == "high"
        assert row["version"] == task.version + 1
        assert len(_events_for(db, task.id)) == 2


def test_the_two_or_more_threshold_is_pinned_on_both_model_surfaces():
    """The routing threshold is pinned as the phrase the model actually reads.

    The description test above asserts "same", "one" and "update_task". Every
    one of those survives a silent edit from "two or more" to "five or more",
    which would change routing without failing anything. The threshold is the
    contract, so the threshold is what gets pinned.

    It pins the word form, which is the form the source uses on both surfaces.
    An earlier name said "numerically" and claimed more precision than the
    assertion has: rewriting "two or more" as "2 or more" would fail this test
    without changing behaviour. That is a false rejection, not a silent bypass,
    and it is the safer direction to err in, so the name was corrected rather
    than the assertion widened.

    Both surfaces are checked because the model sees both and they can drift
    apart. `SYSTEM_PROMPT` is the standing instruction; the tool description is
    what Pydantic AI actually serialises into the request. A threshold stated in
    one and contradicted in the other is worse than a threshold stated in
    neither, because the model gets to pick.

    Deliberately not asserted: that bulk is faster at two targets. It is not
    claimed to be. Bulk owns all-or-nothing semantics for one logical operation,
    which is a correctness property and holds at every size.
    """
    from app import prompts

    threshold = "two or more"

    prompt = prompts.SYSTEM_PROMPT.lower()
    assert threshold in prompt, (
        "SYSTEM_PROMPT no longer states the two-or-more bulk routing threshold; "
        "a different number here silently re-routes multi-task mutations"
    )

    description = _definitions()[ToolName.BULK_UPDATE_TASKS.value].description
    assert threshold in description.lower(), (
        "the model-visible bulk tool description no longer states the "
        "two-or-more threshold"
    )


def test_three_targets_proceed_and_four_require_approval(db):
    """The approval edge is `count > threshold`, so three is allowed and four is not.

    The existing duplicate-reference test proves four references are gated. It
    cannot prove three are not: a policy that gated everything, or a threshold
    quietly lowered to two, would pass it just as happily. Both sides of the
    boundary have to be asserted together or the edge is not pinned at all.

    Three distinct tasks rather than one repeated id, because this is about the
    threshold itself. Duplicate-reference counting is D-79's separate, and
    deliberately different, concern.
    """
    assert settings.blast_radius_threshold == 3, (
        "this test pins the 3/4 edge and must be updated if the default moves"
    )

    run_id = _run()
    allowed = _tasks(db, run_id, 3)

    result = _bulk_through_the_tool(
        run_id,
        [task.id for task in allowed],
        tool_call_id="call-d79-edge-three",
        priority="high",
    )
    assert len(result) == 3, "three targets must proceed without approval"
    for task in allowed:
        assert _row(db, task.id)["priority"] == "high"

    gated = _tasks(db, run_id, 4)
    before = {task.id: _row(db, task.id)["version"] for task in gated}

    with pytest.raises(ApprovalRequired):
        _bulk_through_the_tool(
            run_id,
            [task.id for task in gated],
            tool_call_id="call-d79-edge-four",
            priority="high",
        )

    for task in gated:
        assert _row(db, task.id)["version"] == before[task.id], (
            "a gated bulk call must commit nothing at all, not three of four"
        )
