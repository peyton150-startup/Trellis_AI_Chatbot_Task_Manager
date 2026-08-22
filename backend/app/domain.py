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
from .errors import (
    AppendNotesLimitError,
    BulkTargetCoverageError,
    OutOfScopeError,
    VersionConflictError,
)
from .limits import TASK_NOTES_MAX_CHARS
from .models import (
    BulkUpdateTasksArgs,
    CreateTaskArgs,
    DeleteTasksArgs,
    EventOperation,
    ListTasksArgs,
    MutableTaskFields,
    ResolveTaskReferenceArgs,
    ResolveTaskReferenceResponse,
    TaskReferenceCandidate,
    Task,
    TaskEvent,
    TaskHistoryChange,
    TaskHistoryEffect,
    TaskHistoryEntry,
    TaskHistoryResponse,
    TaskHistoryState,
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
    """Read one owner's tasks with the closed, bounded SQL filters.

    D-77 adds one branch and no new authority. Both statements carry the same
    owner predicate, the same four filters, the same ordering, and the same
    LIMIT; the duplicate variant additionally keeps only rows whose title occurs
    more than once among the rows the filters already selected.

    Which statement runs is decided here rather than by composing a predicate,
    because the duplicate read is a three-stage query whose stage order is its
    correctness condition. See `SELECT_DUPLICATE_TASKS_FOR_OWNER`.

    Duplicate membership is a fact about rows that exist in `tasks` right now.
    `task_events` is not consulted and cannot contribute a member: a deleted task
    keeps its durable history and stops being a current duplicate at the moment
    its row goes away.
    """
    statement = (
        sql.SELECT_DUPLICATE_TASKS_FOR_OWNER
        if arguments.duplicates_only
        else sql.SELECT_TASKS_FOR_OWNER
    )
    rows = conn.execute(
        statement,
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


# `SELECT_TASK_REFERENCE_CANDIDATES` emits 0 for a case-insensitive exact title
# and 1 for a substring hit. Named here so the comparison below reads as a rule
# rather than as a magic number.
_EXACT_TITLE_MATCH = 0


def resolve_task_reference(
    owner_id: UUID,
    arguments: ResolveTaskReferenceArgs,
    *,
    conn: Connection,
) -> ResolveTaskReferenceResponse:
    """Resolve a bounded current-or-historical title reference for one owner.

    Deterministic code owns this decision, not the model. The rule is that one
    exact title outranks any number of weaker substring matches, while two exact
    task ids stay ambiguous:

        exactly one exact task id   -> resolve it
        two or more exact task ids  -> ambiguous
        no exact, one candidate     -> resolve it
        no exact, many candidates   -> ambiguous

    Exactness is read from the query's `match_rank` rather than recomputed here.
    PostgreSQL's `lower(...)` and Python's `str.lower()` do not agree on every
    input, and two definitions of "exact" that disagree on one title is exactly
    the kind of drift this boundary exists to prevent.

    `match_rank` stays internal. Candidates are built from the public columns
    only, so the wire model never carries a ranking the caller could mistake for
    a decision it should make itself.
    """
    rows = conn.execute(
        sql.SELECT_TASK_REFERENCE_CANDIDATES,
        {
            "owner_id": owner_id,
            "reference": arguments.reference,
            "limit": arguments.limit,
        },
    ).fetchall()

    candidates: list[TaskReferenceCandidate] = []
    exact_task_ids: set[UUID] = set()
    for row in rows:
        candidate = TaskReferenceCandidate(
            task_id=row["task_id"],
            matched_title=row["matched_title"],
            current_title=row["current_title"],
            current_version=row["current_version"],
            exists_now=row["exists_now"],
        )
        candidates.append(candidate)
        if row["match_rank"] == _EXACT_TITLE_MATCH:
            exact_task_ids.add(candidate.task_id)

    # Rows are already deduplicated per task id, so each id appears at most
    # once and these counts are counts of tasks rather than of matched strings.
    resolved: TaskReferenceCandidate | None = None
    if len(exact_task_ids) == 1:
        resolved = next(
            item for item in candidates if item.task_id in exact_task_ids
        )
    elif not exact_task_ids and len(candidates) == 1:
        resolved = candidates[0]

    return ResolveTaskReferenceResponse(
        reference=arguments.reference,
        resolved=resolved,
        candidates=candidates,
    )


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
    """Guard one update by its expected version and capture both full rows.

    Staleness is decided before any validation that depends on current state,
    and the order is the point rather than an optimisation. `_effective_update`
    measures an append against the notes this transaction just locked, so a
    caller holding a stale version could be told its addition is too long when
    the fact it actually needs is that its version moved. Those two refusals
    ask for different next actions: shorten the text, versus refresh and look
    again. Once the lock has succeeded, the row already says which one is true.

    The `FOR UPDATE` above is what makes the early comparison safe. It blocks
    other writers on this row until the transaction ends, and a caller that had
    to wait for a competing transaction is handed the row as that transaction
    left it, so `before.version` is current rather than a pre-lock guess.

    This does not replace optimistic concurrency. `UPDATE_TASK_GUARDED` keeps
    its own `version = expected_version` predicate as the fail-closed database
    invariant; this comparison only decides which truthful refusal the caller
    hears first.
    """
    locked = _locked_tasks(owner_id, (arguments.task_id,), conn=conn)
    before = locked.get(arguments.task_id)

    # A missing row and another actor's row are both `None` here, and they stay
    # indistinguishable. Only an owned, locked row can be compared, so the two
    # out-of-scope cases fall through to the guarded UPDATE and refuse exactly
    # as they did before, disclosing nothing new.
    if before is not None and before.version != arguments.expected_version:
        raise VersionConflictError()

    effective = _effective_update(arguments, before)

    row = conn.execute(
        sql.UPDATE_TASK_GUARDED,
        _update_parameters(
            owner_id,
            arguments.task_id,
            arguments.expected_version,
            effective,
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
    count at the policy layer under D-17, while set membership mutates one
    physical row once.

    D-79 replaced a loop that issued one guarded UPDATE per target with one
    guarded UPDATE over the whole set. The guards did not change: the same owner
    predicate and the same per-row version predicate decide every row, and a row
    whose version moved still matches nothing. What changed is that the
    expectations travel as a relation rather than as N separate statements.

    State the improvement narrowly, because it is narrow. The number of task
    UPDATE statements this module issues is constant with the target count,
    N to 1. PostgreSQL still processes N target rows, and ``write_events``
    still issues one audit INSERT per physical task, so the work the database
    does has not become constant and this transaction has not become O(1) in
    any sense worth claiming.

    The audit rows are deliberately left alone rather than left alone because
    they must be. PostgreSQL can insert many rows in one INSERT and RETURN
    information for all of them, so batching them is possible; it is simply not
    what this decision does, and it would have to prove per-task snapshot
    fidelity and its own measured benefit first.
    """
    task_ids = _unique_ids(arguments.task_ids)
    if not task_ids:
        return MutationResult(tasks=(), events=())

    locked = _locked_tasks(owner_id, task_ids, conn=conn)
    _require_all_targets(task_ids, locked)

    # D-80. Append is a different execution mode, not a different value passed
    # to the same statement, so it branches here. Everything above is shared:
    # the same deduplication, the same canonical lock, the same coverage
    # requirement. Everything below differs, because append needs a per-target
    # effective value merged from locked state.
    if arguments.append_notes is not None:
        return _bulk_append_notes(owner_id, task_ids, locked, arguments, conn=conn)

    update_ids, expected_versions = _expected_relation(task_ids, locked)

    rows = conn.execute(
        sql.BULK_UPDATE_TASKS_GUARDED,
        _bulk_update_parameters(owner_id, update_ids, expected_versions, arguments),
    ).fetchall()

    # RETURNING has no ordering to rely on, so the rows are keyed by id and the
    # caller-visible order is rebuilt from the request. Coverage is the guard
    # that replaces the old loop's per-statement `row is None`: anything short
    # of every expected target means a row moved under the lock, and the caller
    # transaction rolls back rather than committing a partial set.
    updated_by_id = {task.id: task for task in (_task(row) for row in rows)}
    if set(updated_by_id) != set(update_ids):
        raise VersionConflictError()

    updated = tuple(updated_by_id[task_id] for task_id in task_ids)
    events = tuple(
        _updated_event(locked[task_id], updated_by_id[task_id]) for task_id in task_ids
    )
    return MutationResult(tasks=updated, events=events)


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

    snapshot: TaskHistoryState | None
    if before is None and after is not None:
        effect = TaskHistoryEffect.CREATED
        snapshot = _history_state(after)
    elif before is not None and after is not None:
        effect = TaskHistoryEffect.UPDATED
        snapshot = None
    elif before is not None and after is None:
        effect = TaskHistoryEffect.DELETED
        snapshot = _history_state(before)
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
        snapshot=snapshot,
        changes=changes,
    )


def _history_state(task: Task) -> TaskHistoryState:
    return TaskHistoryState.model_validate(
        task.model_dump(
            mode="json",
            include=set(TaskHistoryState.model_fields),
        )
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
    return {
        "id": task_id,
        "owner_id": owner_id,
        "expected_version": expected_version,
        **_mutable_field_parameters(arguments),
    }


def _bulk_append_notes(
    owner_id: UUID,
    task_ids: Sequence[UUID],
    locked: dict[UUID, Task],
    arguments: BulkUpdateTasksArgs,
    *,
    conn: Connection,
) -> MutationResult:
    """Append one fragment to every locked target, all of them or none.

    The model sends only its new text, exactly as D-78 established for a single
    task, and each existing value comes from the row this transaction holds a
    lock on. Nothing the model read earlier can be stale by the time the merge
    happens, and a note the model never saw is not overwritten.

    The ordering below is the whole contract. Every merged value is computed and
    validated for the entire target set *before* the mutating statement runs, so
    a set where nine merges fit and the tenth overflows commits nothing at all.
    Merging and updating target by target would leave nine appended notes and a
    failed run, which is precisely the partial outcome this decision exists to
    remove, and it is what the per-task loop produced in practice.

    `merge_appended_notes` is reused rather than reimplemented. The separator
    rule is D-78's and there must be exactly one of it.
    """
    relation = _bulk_append_relation(task_ids, locked, arguments.append_notes)

    rows = conn.execute(
        sql.BULK_APPEND_NOTES_GUARDED,
        {
            "owner_id": owner_id,
            "task_ids": [task_id for task_id, _, _ in relation],
            "expected_versions": [version for _, version, _ in relation],
            "effective_notes": [notes for _, _, notes in relation],
        },
    ).fetchall()

    updated_by_id = {task.id: task for task in (_task(row) for row in rows)}
    if set(updated_by_id) != {task_id for task_id, _, _ in relation}:
        # Same fail-closed rule as the replacement path, but reported as the
        # bulk-specific subtype: this caller supplied no expected version, so
        # advice to refresh one and retry would be advice it cannot act on.
        raise BulkTargetCoverageError()

    updated = tuple(updated_by_id[task_id] for task_id in task_ids)
    events = tuple(
        _updated_event(locked[task_id], updated_by_id[task_id]) for task_id in task_ids
    )
    return MutationResult(tasks=updated, events=events)


def _bulk_append_relation(
    task_ids: Sequence[UUID],
    locked: dict[UUID, Task],
    fragment: str,
) -> tuple[tuple[UUID, int, str], ...]:
    """Merge and validate every target before any of them is written.

    Returns one triple per distinct target, built as a single relation for the
    reason `_expected_relation` records at length: three parallel arrays that
    are assembled separately can disagree, and `unnest` NULL-pads rather than
    refusing, so the mistake would surface later wearing the wrong name.

    The size check runs against the merged value rather than the fragment. A
    legal fragment can still produce an illegal note, and that is exactly the
    case the schema cannot see.
    """
    relation = tuple(
        (task_id, locked[task_id].version, merge_appended_notes(locked[task_id].notes, fragment))
        for task_id in task_ids
    )

    if len(relation) != len(task_ids):
        raise RuntimeError("bulk append built a relation of the wrong size")
    if len({task_id for task_id, _, _ in relation}) != len(relation):
        raise RuntimeError("bulk append built a duplicated expected target")
    if any(version is None for _, version, _ in relation):
        raise RuntimeError("bulk append built a null expected version")
    if any(notes is None for _, _, notes in relation):
        raise RuntimeError("bulk append built a null effective note")

    # Every target is checked, and the first failure refuses the whole call.
    # Truncating instead would silently discard the caller's text while still
    # committing a version increment and an event, recording a mutation that
    # does not say what happened.
    for task_id, _, notes in relation:
        if len(notes) > TASK_NOTES_MAX_CHARS:
            raise AppendNotesLimitError(
                f"appending would make one task's notes {len(notes)} characters, "
                f"over the {TASK_NOTES_MAX_CHARS} limit; no task was changed"
            )

    return relation


def _expected_relation(
    task_ids: Sequence[UUID],
    locked: dict[UUID, Task],
) -> tuple[list[UUID], list[int]]:
    """Build the id and expected-version arrays the bulk statement joins on.

    One relation, unzipped, rather than two lists assembled side by side. The
    arrays reach SQL as an expected-version lookup keyed by id, and if they ever
    disagreed on length or order the statement would still run. `unnest` over
    two arrays NULL-pads the shorter one rather than rejecting it, and
    `version = NULL` matches no row, so a dropped expectation fails closed but
    arrives disguised as a target-coverage conflict, which describes the wrong
    problem and sends the reader to the wrong place.

    Deriving both arrays from one canonical relation prevents ordinary
    construction drift here. It does not make drift unwritable, because the two
    arrays are separate values from the moment this function returns them, and
    `_bulk_update_parameters` accepts them as independent arguments. So that
    function reasserts equal cardinality on the values actually crossing into
    SQL, and this one validates the construction. Two boundaries, two different
    failures.

    The checks below cover what deriving the arrays together cannot: that
    nothing upstream handed this function a malformed target set. They are
    defence in depth and are expected to be unreachable through
    `bulk_update_tasks`, where `_unique_ids` and `_require_all_targets` have
    already run. `RuntimeError` rather than a domain error is deliberate: a
    failure here is a bug in this module, not a refusal the caller can act on.
    """
    expected_pairs = tuple((task_id, locked[task_id].version) for task_id in task_ids)
    update_ids = [task_id for task_id, _ in expected_pairs]
    expected_versions = [version for _, version in expected_pairs]

    if not (
        len(expected_pairs)
        == len(update_ids)
        == len(expected_versions)
        == len(task_ids)
    ):
        raise RuntimeError("bulk update built an expected relation of the wrong size")
    if len(set(update_ids)) != len(update_ids):
        # Trellis must ensure at most one expected source row joins each target
        # row. PostgreSQL does not reject an UPDATE ... FROM whose join matches
        # several source rows: it uses one of them, and which one is not
        # predictable. So the uniqueness is this module's obligation, not
        # something the database will refuse on our behalf.
        raise RuntimeError("bulk update built a duplicated expected target")
    if any(task_id is None for task_id in update_ids):
        raise RuntimeError("bulk update built a null expected target")
    if any(version is None for version in expected_versions):
        raise RuntimeError("bulk update built a null expected version")

    return update_ids, expected_versions


def _bulk_update_parameters(
    owner_id: UUID,
    task_ids: Sequence[UUID],
    expected_versions: Sequence[int],
    arguments: BulkUpdateTasksArgs,
) -> dict[str, object]:
    """Bind one bulk statement, sharing the field rules with the single update.

    The SET list is the only thing the two statements have in common that could
    drift, and drift here is invisible: a bulk call that stopped honouring the
    omitted-versus-null contract would clear due dates nobody mentioned while
    every single-task test stayed green. One builder makes that impossible to
    do by halves.

    This is also the last boundary before the arrays become SQL parameters, and
    it is the only place that sees the exact values being bound. `unnest` over
    two arrays NULL-pads the shorter one instead of rejecting it, so unequal
    lengths would execute and fail closed somewhere else, described as
    something else. `_expected_relation` validates its own construction; this
    validates what is actually crossing the boundary, whoever built it.
    """
    if len(task_ids) != len(expected_versions):
        raise RuntimeError("bulk update id/version relation length mismatch")

    return {
        "owner_id": owner_id,
        "task_ids": list(task_ids),
        "expected_versions": list(expected_versions),
        **_mutable_field_parameters(arguments),
    }


def _mutable_field_parameters(
    arguments: UpdateTaskArgs | BulkUpdateTasksArgs,
) -> dict[str, object]:
    fields = arguments.model_fields_set
    return {
        "title": arguments.title if "title" in fields else None,
        "notes": arguments.notes if "notes" in fields else None,
        "due_date": arguments.due_date,
        "set_due_date": "due_date" in fields,
        "priority": _enum_value(arguments.priority) if "priority" in fields else None,
        "status": _enum_value(arguments.status) if "status" in fields else None,
        "blocked_by": arguments.blocked_by,
        "set_blocked_by": "blocked_by" in fields,
    }


def merge_appended_notes(existing: str, addition: str) -> str:
    """Join an appended fragment to the notes already stored.

    One newline separates two notes, and only when one is needed. Existing text
    that already ends in a newline supplies its own separator, and empty notes
    need none at all.

    The fragment is otherwise preserved byte for byte. No bullet, no numbering,
    no punctuation, and no blank line is invented, because the caller asked to
    add their text rather than to have it formatted. A leading newline the
    caller actually supplied is theirs and survives: "alpha" plus "\\nbeta" is
    "alpha\\n\\nbeta", a deliberate blank line, not a separator to collapse.
    """
    if existing == "":
        return addition
    if existing.endswith("\n"):
        return existing + addition
    return existing + "\n" + addition


def _effective_update(
    arguments: UpdateTaskArgs,
    before: Task | None,
) -> UpdateTaskArgs:
    """Resolve an append request against locked state into a plain replacement.

    D-78 moves note appending out of the model and into deterministic code. The
    model sends only its new fragment; the authoritative current value comes
    from the row this transaction already holds a lock on, so no read the model
    performed earlier can be stale by the time the merge happens.

    The transformation must not disturb the omitted-versus-null contract that
    `_update_parameters` reads from `model_fields_set`. Rebuilding the model
    through `model_validate(model_dump())` would mark every field as set, after
    which a request that never mentioned `due_date` would clear it. `model_copy`
    preserves the existing set and adds only the key supplied here.

    `model_copy` does not validate, so the merged value is validated first and
    the copy carries an already-checked string. Validating the fragment alone
    would not do: the fragment can be legal while the merged note is not.
    """
    if arguments.append_notes is None:
        return arguments

    # No locked row means the target is missing or foreign. Leave the arguments
    # alone and let the guarded UPDATE produce the ordinary conflict, so an
    # append cannot be told apart from a replacement by its failure.
    if before is None:
        return arguments

    merged = merge_appended_notes(before.notes, arguments.append_notes)
    if len(merged) > TASK_NOTES_MAX_CHARS:
        # Refuse rather than truncate. Truncation would silently discard the
        # caller's text and still commit a version increment and an event,
        # recording a mutation that does not say what happened.
        #
        # D-80 narrowed the type. This is still a VALIDATION_ERROR with the same
        # 422, so no code was added, but the single-task and bulk append paths
        # now raise one recognisable class for the same condition, which is what
        # lets the model adapter treat this one validation failure as terminal
        # without treating every validation failure that way.
        raise AppendNotesLimitError(
            f"appending would make notes {len(merged)} characters, over the "
            f"{TASK_NOTES_MAX_CHARS} limit"
        )

    return arguments.model_copy(update={"notes": merged})


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
