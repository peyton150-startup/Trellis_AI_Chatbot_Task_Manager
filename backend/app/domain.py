"""Task domain services and append-only event preparation.

This module is the only writer of ``tasks`` and ``task_events``. Every public
function takes the database connection explicitly as a required keyword-only
argument. Mutations, their event rows, and ``idempotency.complete`` therefore
run on the same caller-owned transaction. Nothing here commits or rolls back.

Task rows are authoritative current state. Event snapshots are JSON-safe,
complete task rows so T07 can compare ``after["version"]`` and restore a
deleted task with ``before["id"]`` and every other original field.

Two entry points exist for T07 alone and are not tool paths.
``delete_task_guarded`` and ``restore_task`` carry the compensating writes undo
needs, and they live here rather than in ``undo.py`` so that this module remains
the only code that writes either table. See D-39 and D-41.

Since T00L one further write is coupled to this module without originating in
it. An ``AFTER INSERT`` trigger on ``task_events`` enqueues a
``linear_projections`` row inside the same transaction, under D-25. That is the
intended shape: the ownership rule is that this module owns task business state
and business ``task_events``, while Linear projection and reconciliation
metadata is structurally separate integration state living in its own tables. No
function here reads or writes those tables, integration bookkeeping never
increments a task version and never produces a business event, and nothing about
that state can enter a ``Task``, because ``TrellisModel`` forbids extra keys and
the integration columns are not on ``tasks``. See D-26.

One mutation in this schema does not originate here. ``tasks.blocked_by`` is a
self reference declared ``ON DELETE SET NULL``, so deleting a task rewrites
every surviving row that pointed at it, without passing through any function
below. ``delete_tasks`` snapshots those rows before the delete and events the
cascade as ordinary updates, so the audit log stays a complete account of how
current state was reached and undo has something to reverse.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Json
from pydantic import JsonValue

from . import sql
from .errors import OutOfScopeError, VersionConflictError
from .models import (
    BulkUpdateTasksArgs,
    CreateTaskArgs,
    DeleteTasksArgs,
    EventOperation,
    ListTasksArgs,
    MutableTaskFields,
    Task,
    TaskEvent,
    TaskHistoryChange,
    TaskHistoryEffect,
    TaskHistoryEntry,
    TaskHistoryResponse,
    UpdateTaskArgs,
)


TaskSnapshot = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class PendingTaskEvent:
    """An event payload prepared beside its mutation but not yet persisted."""

    task_id: UUID
    operation: EventOperation
    before: TaskSnapshot | None
    after: TaskSnapshot | None


@dataclass(frozen=True, slots=True)
class MutationResult:
    """Rows returned by a mutation and the events the caller must write.

    ``tasks`` contains the post-mutation shape for creates and updates, and the
    deleted shape for deletes. ``events`` is passed unchanged to
    :func:`write_events` before the caller commits the transaction.
    """

    tasks: tuple[Task, ...]
    events: tuple[PendingTaskEvent, ...]


def list_tasks(
    owner_id: UUID,
    arguments: ListTasksArgs,
    *,
    conn: Connection,
) -> list[Task]:
    """Read one owner's tasks with the closed, bounded SQL filters."""
    rows = conn.execute(
        sql.SELECT_TASKS_FOR_OWNER,
        {
            "owner_id": owner_id,
            "status": _enum_value(arguments.status),
            "due_before": arguments.due_before,
            "due_after": arguments.due_after,
            "priority": _enum_value(arguments.priority),
            "limit": arguments.limit,
        },
    ).fetchall()
    return [_task(row) for row in rows]


def create_task(
    owner_id: UUID,
    arguments: CreateTaskArgs,
    *,
    conn: Connection,
) -> MutationResult:
    """Insert one task and prepare its complete ``created`` event."""
    row = conn.execute(
        sql.INSERT_TASK,
        {
            "owner_id": owner_id,
            "title": arguments.title,
            "notes": arguments.notes,
            "due_date": arguments.due_date,
            "priority": arguments.priority.value,
            "blocked_by": arguments.blocked_by,
        },
    ).fetchone()
    task = _task(row)
    event = PendingTaskEvent(
        task_id=task.id,
        operation=EventOperation.CREATED,
        before=None,
        after=_snapshot(task),
    )
    return MutationResult(tasks=(task,), events=(event,))


def update_task(
    owner_id: UUID,
    arguments: UpdateTaskArgs,
    *,
    conn: Connection,
) -> MutationResult:
    """Guard one update by its expected version and capture both full rows."""
    locked = _locked_tasks(owner_id, (arguments.task_id,), conn=conn)
    before = locked.get(arguments.task_id)

    row = conn.execute(
        sql.UPDATE_TASK_GUARDED,
        _update_parameters(
            owner_id,
            arguments.task_id,
            arguments.expected_version,
            arguments,
        ),
    ).fetchone()
    if row is None or before is None:
        raise VersionConflictError()

    after = _task(row)
    event = _updated_event(before, after)
    return MutationResult(tasks=(after,), events=(event,))


def bulk_update_tasks(
    owner_id: UUID,
    arguments: BulkUpdateTasksArgs,
    *,
    conn: Connection,
) -> MutationResult:
    """Lock and update each distinct target once in the caller's transaction.

    ``BulkUpdateTasksArgs`` has no expected-version field. The locked current
    version becomes each guarded UPDATE's expected version. Duplicate ids still
    count at the policy layer under D-17, while SQL ``ANY`` semantics mutate one
    physical row once.
    """
    task_ids = _unique_ids(arguments.task_ids)
    if not task_ids:
        return MutationResult(tasks=(), events=())

    locked = _locked_tasks(owner_id, task_ids, conn=conn)
    _require_all_targets(task_ids, locked)

    updated: list[Task] = []
    events: list[PendingTaskEvent] = []
    for task_id in task_ids:
        before = locked[task_id]
        row = conn.execute(
            sql.UPDATE_TASK_GUARDED,
            _update_parameters(owner_id, task_id, before.version, arguments),
        ).fetchone()
        if row is None:
            raise VersionConflictError()
        after = _task(row)
        updated.append(after)
        events.append(_updated_event(before, after))

    return MutationResult(tasks=tuple(updated), events=tuple(events))


def delete_tasks(
    owner_id: UUID,
    arguments: DeleteTasksArgs,
    *,
    conn: Connection,
) -> MutationResult:
    """Delete every distinct target or fail before allowing a partial result.

    ``tasks`` carries the deleted rows only. A delete can also clear
    ``blocked_by`` on rows that were never targets, through the schema's
    ``ON DELETE SET NULL``. Those rows are not part of the tool's result, but
    they do get their own ``updated`` events, because an unrecorded mutation is
    one the audit log cannot explain and undo cannot reverse.
    """
    task_ids = _unique_ids(arguments.task_ids)
    if not task_ids:
        return MutationResult(tasks=(), events=())

    locked = _locked_tasks(owner_id, task_ids, conn=conn)
    _require_all_targets(task_ids, locked)

    # Snapshot the rows the cascade is about to rewrite, while they still point
    # at their blocker and while this transaction holds their locks.
    blocked_before = _tasks_blocked_by(owner_id, task_ids, conn=conn)

    rows = conn.execute(
        sql.DELETE_TASKS_BY_IDS,
        {"owner_id": owner_id, "task_ids": list(task_ids)},
    ).fetchall()
    deleted_tasks = tuple(_task(row) for row in rows)
    deleted_by_id = {task.id: task for task in deleted_tasks}
    _require_all_targets(task_ids, deleted_by_id)

    deleted = tuple(deleted_by_id[task_id] for task_id in task_ids)
    deleted_events = tuple(
        PendingTaskEvent(
            task_id=task.id,
            operation=EventOperation.DELETED,
            before=_snapshot(task),
            after=None,
        )
        for task in deleted
    )
    cascade_events = _cascade_events(owner_id, blocked_before, conn=conn)

    # Cleared-pointer events first, deleted events last, so their ids ascend in
    # that order. Section 8 applies undo in reverse id order, so the deleted
    # events are undone first and every blocker is back in the table before any
    # pointer to it is restored. The opposite order would write a foreign key
    # reference to a row that does not exist yet.
    return MutationResult(tasks=deleted, events=cascade_events + deleted_events)


def delete_task_guarded(
    owner_id: UUID,
    task_id: UUID,
    expected_version: int,
    *,
    conn: Connection,
) -> MutationResult:
    """Delete one task only if its version still matches, for T07 under D-39.

    ``delete_tasks`` is the tool path and carries no version predicate, which is
    correct there because the policy check and the row lock run immediately
    before it in the same transaction. Undo establishes the version in an
    earlier precheck pass, so it needs the guard on the write itself or a
    concurrent change lands inside the window and is destroyed rather than
    refused.

    The delete cascade is audited exactly as ``delete_tasks`` audits it. See
    D-41: one inverse operation may legitimately emit one direct compensation
    event plus N cascade events, because suppressing the cascade would reopen
    the audit hole D-23 closed.
    """
    # Snapshot and lock the referencing rows while they still point at the
    # target, before ON DELETE SET NULL rewrites them.
    blocked_before = _tasks_blocked_by(owner_id, (task_id,), conn=conn)

    row = conn.execute(
        sql.DELETE_TASK_GUARDED,
        {
            "id": task_id,
            "owner_id": owner_id,
            "expected_version": expected_version,
        },
    ).fetchone()
    if row is None:
        # The row moved, vanished, or is not this owner's. All three are a
        # concurrent-state change to the caller, which fails closed.
        raise VersionConflictError()

    deleted = _task(row)
    deleted_event = PendingTaskEvent(
        task_id=deleted.id,
        operation=EventOperation.DELETED,
        before=_snapshot(deleted),
        after=None,
    )
    cascade_events = _cascade_events(owner_id, blocked_before, conn=conn)
    # Cleared pointers first, the deletion last, matching delete_tasks so that
    # persisted ids ascend in the order a later reverse-order undo needs.
    return MutationResult(tasks=(deleted,), events=cascade_events + (deleted_event,))


def restore_task(
    owner_id: UUID,
    snapshot: TaskSnapshot,
    *,
    version: int,
    conn: Connection,
) -> MutationResult:
    """Re-insert a deleted task under its original id, for T07 under D-39.

    The caller supplies ``version`` rather than this function deriving it.
    Section 8 owns the rule that a restored task continues from the deleted
    row's version plus one, and that is undo semantics; this layer executes the
    write and reports what the database produced.

    The emitted event carries ``created``, which is the physical operation. Undo
    relabels the direct compensation to ``restored`` under D-41, so that the
    operation this layer reports always describes the write it actually made.
    """
    row = conn.execute(
        sql.INSERT_TASK_RESTORED,
        {
            "id": snapshot["id"],
            "owner_id": owner_id,
            "title": snapshot["title"],
            "notes": snapshot["notes"],
            "due_date": snapshot["due_date"],
            "priority": snapshot["priority"],
            "status": snapshot["status"],
            "blocked_by": snapshot["blocked_by"],
            "version": version,
            "created_at": snapshot["created_at"],
        },
    ).fetchone()
    task = _task(row)
    event = PendingTaskEvent(
        task_id=task.id,
        operation=EventOperation.CREATED,
        before=None,
        after=_snapshot(task),
    )
    return MutationResult(tasks=(task,), events=(event,))


def write_events(
    run_id: UUID,
    actor_id: UUID,
    events: Iterable[PendingTaskEvent],
    *,
    conn: Connection,
) -> list[TaskEvent]:
    """Persist prepared events without ending the caller's transaction."""
    written: list[TaskEvent] = []
    for event in events:
        row = conn.execute(
            sql.INSERT_TASK_EVENT,
            {
                "task_id": event.task_id,
                "run_id": run_id,
                "actor_id": actor_id,
                "operation": event.operation.value,
                "before": Json(event.before) if event.before is not None else None,
                "after": Json(event.after) if event.after is not None else None,
            },
        ).fetchone()
        written.append(TaskEvent.model_validate(row))
    return written


def read_events(
    run_id: UUID,
    *,
    limit: int,
    conn: Connection,
) -> list[TaskEvent]:
    """Read one bounded page of a run's events, newest first."""
    rows = conn.execute(
        sql.SELECT_EVENTS_FOR_RUN,
        {"run_id": run_id, "limit": limit},
    ).fetchall()
    return [TaskEvent.model_validate(row) for row in rows]


def read_task_history(
    actor_id: UUID,
    task_id: UUID,
    *,
    limit: int,
    before_event_id: int | None,
    conn: Connection,
) -> TaskHistoryResponse:
    """Read one actor-scoped page of durable task history, newest first.

    Audit rows authorize deleted history directly. A current owned task with no
    events is also valid because the administrative seed path predates ordinary
    event creation. If neither form of ownership evidence exists, missing and
    foreign ids fail identically.
    """
    scope = conn.execute(
        sql.SELECT_TASK_HISTORY_SCOPE,
        {"task_id": task_id, "actor_id": actor_id},
    ).fetchone()
    if scope is None:
        raise RuntimeError("task history scope query returned no row")

    current_version = scope["current_version"]
    if not scope["has_events"] and current_version is None:
        raise OutOfScopeError()

    rows = conn.execute(
        sql.SELECT_TASK_EVENTS_FOR_ACTOR,
        {
            "task_id": task_id,
            "actor_id": actor_id,
            "before_event_id": before_event_id,
            "limit": limit + 1,
        },
    ).fetchall()
    events = [TaskEvent.model_validate(row) for row in rows]

    has_older = len(events) > limit
    page = events[:limit]
    entries = [_history_entry(event) for event in page]

    return TaskHistoryResponse(
        task_id=task_id,
        exists_now=current_version is not None,
        current_version=current_version,
        entries=entries,
        next_before_event_id=(
            entries[-1].event_id if has_older and entries else None
        ),
    )


def _history_entry(event: TaskEvent) -> TaskHistoryEntry:
    before = _history_task(event.before)
    after = _history_task(event.after)

    if before is None and after is not None:
        effect = TaskHistoryEffect.CREATED
    elif before is not None and after is not None:
        effect = TaskHistoryEffect.UPDATED
    elif before is not None and after is None:
        effect = TaskHistoryEffect.DELETED
    else:
        raise RuntimeError("task event has neither a before nor after snapshot")

    changes: list[TaskHistoryChange] = []
    if before is not None and after is not None:
        before_fields = before.model_dump(
            mode="json", include=set(MutableTaskFields.model_fields)
        )
        after_fields = after.model_dump(
            mode="json", include=set(MutableTaskFields.model_fields)
        )
        for field in MutableTaskFields.model_fields:
            if before_fields[field] != after_fields[field]:
                changes.append(
                    TaskHistoryChange(
                        field=field,
                        before=before_fields[field],
                        after=after_fields[field],
                    )
                )

    return TaskHistoryEntry(
        event_id=event.id,
        operation=event.operation,
        effect=effect,
        occurred_at=event.created_at,
        version_before=before.version if before is not None else None,
        version_after=after.version if after is not None else None,
        changes=changes,
    )


def _history_task(snapshot: JsonValue | None) -> Task | None:
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        raise RuntimeError("task event snapshot is not an object")
    return Task.model_validate(snapshot)


def _locked_tasks(
    owner_id: UUID,
    task_ids: Sequence[UUID],
    *,
    conn: Connection,
) -> dict[UUID, Task]:
    if not task_ids:
        return {}
    rows = conn.execute(
        sql.SELECT_TASKS_BY_IDS_FOR_UPDATE,
        {"owner_id": owner_id, "task_ids": list(task_ids)},
    ).fetchall()
    tasks = (_task(row) for row in rows)
    return {task.id: task for task in tasks}


def _tasks_blocked_by(
    owner_id: UUID,
    task_ids: Sequence[UUID],
    *,
    conn: Connection,
) -> tuple[Task, ...]:
    """Lock and return the rows whose blocked_by points into ``task_ids``."""
    rows = conn.execute(
        sql.SELECT_TASKS_BLOCKED_BY_IDS,
        {"owner_id": owner_id, "task_ids": list(task_ids)},
    ).fetchall()
    return tuple(_task(row) for row in rows)


def _cascade_events(
    owner_id: UUID,
    before_rows: Sequence[Task],
    *,
    conn: Connection,
) -> tuple[PendingTaskEvent, ...]:
    """Turn the delete cascade into ordinary updated events.

    Called after the delete, once the database has applied ON DELETE SET NULL.
    The rows are re-read rather than reconstructed, so the after snapshot is the
    committed shape rather than this module's guess at it. ``version`` is
    unchanged, because a foreign key action does not run the guarded update, and
    that is what undo compares against.
    """
    if not before_rows:
        return ()

    after_by_id = _locked_tasks(owner_id, [task.id for task in before_rows], conn=conn)
    events: list[PendingTaskEvent] = []
    for before in before_rows:
        after = after_by_id.get(before.id)
        if after is None:
            # The row vanished between the snapshot and here despite the lock,
            # which is not reachable inside one transaction. Fail closed.
            raise VersionConflictError()
        if after.blocked_by == before.blocked_by:
            continue
        events.append(_updated_event(before, after))
    return tuple(events)


def _require_all_targets(
    task_ids: Sequence[UUID], tasks_by_id: dict[UUID, Task]
) -> None:
    if len(tasks_by_id) != len(task_ids):
        # policy.check normally catches missing and foreign rows before this
        # layer. A row disappearing between that check and this transaction is
        # a concurrent-state change, so fail closed with the existing conflict.
        raise VersionConflictError()


def _update_parameters(
    owner_id: UUID,
    task_id: UUID,
    expected_version: int,
    arguments: UpdateTaskArgs | BulkUpdateTasksArgs,
) -> dict[str, object]:
    # due_date and blocked_by are the two fields whose null is a value rather
    # than an absence, so UPDATE_TASK_GUARDED gates them on a set flag instead of
    # COALESCE. The flag can only come from model_fields_set, and that carries a
    # caller contract: pass arguments validated from the payload the caller
    # actually received. Revalidating a full model_dump marks every field as set,
    # after which an update that never mentioned due_date clears it. Undo is the
    # one caller that legitimately sets every field, because restoring a complete
    # before snapshot is exactly what section 8 asks it to do.
    fields = arguments.model_fields_set
    return {
        "id": task_id,
        "owner_id": owner_id,
        "expected_version": expected_version,
        "title": arguments.title if "title" in fields else None,
        "notes": arguments.notes if "notes" in fields else None,
        "due_date": arguments.due_date,
        "set_due_date": "due_date" in fields,
        "priority": _enum_value(arguments.priority) if "priority" in fields else None,
        "status": _enum_value(arguments.status) if "status" in fields else None,
        "blocked_by": arguments.blocked_by,
        "set_blocked_by": "blocked_by" in fields,
    }


def _updated_event(before: Task, after: Task) -> PendingTaskEvent:
    return PendingTaskEvent(
        task_id=after.id,
        operation=EventOperation.UPDATED,
        before=_snapshot(before),
        after=_snapshot(after),
    )


def _unique_ids(task_ids: Sequence[UUID]) -> tuple[UUID, ...]:
    return tuple(dict.fromkeys(task_ids))


def _task(row) -> Task:
    if row is None:
        raise RuntimeError("task statement returned no row")
    return Task.model_validate(row)


def _snapshot(task: Task) -> TaskSnapshot:
    return task.model_dump(mode="json")


def _enum_value(value: Enum | None) -> str | None:
    return value.value if value is not None else None
