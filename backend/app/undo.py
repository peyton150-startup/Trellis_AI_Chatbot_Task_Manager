"""KERNEL. Compensating mutation with version guards.

Transcribed from BUILD_SPEC section 8. The precheck-then-apply split is the
whole point: a version that applies as it goes produces partial undo, which is
worse than no undo.

Two corrections to the printed text are recorded as decisions rather than
invented here.

D-38 fixes the precheck. Section 8 compares every event against current
database state, which is unimplementable for any run that touched one task more
than once: a run that creates a task and then updates it leaves the create
event's ``after["version"]`` one behind the current row, and a run that updates
a task and then deletes it leaves the update event demanding a row its own
delete removed. Both refuse a run nothing else touched. The precheck therefore
walks the events in the same reverse order the apply pass uses and compares each
against projected state, which is identical to the literal reading for every run
that touches each task once. Two projections are maintained, and they are not
the same number. The precheck tracks the historical version carried in the
snapshots; the apply pass tracks the physical version in the row, which moves
forward as each compensation lands because history is append-only and never
rewound.

D-39 covers the three statements section 5 does not list, and the two domain
entry points that execute them.

D-41 covers the event boundary. This module builds and relabels compensation
event payloads; every write to ``tasks`` and ``task_events`` goes through
``domain``.

D-27 adds the divergence refusal at T00L, and its boundary is narrower than it
sounds. This module never calls Linear and never depends on Linear being
reachable; it reads one local boolean out of `linear_task_state` during the
precheck and refuses on it. Reading a local conflict marker is not performing an
integration. It never writes that table, so a refusal leaves the marker set and
a later successful undo cannot silently clear the evidence that something
outside the system moved.

Scope, from section 8 and D-38: one run, all or nothing, no partial undo and no
cross-run undo. Repeated invocation is not redo. Compensation events keep the
original ``run_id`` for audit correlation, which means a second call would load
both waves, and undoing that combined history is not a well-defined inverse of
anything. A run that already carries compensation events is no longer eligible,
and ``RunDetail.can_undo`` is where that eligibility is enforced. This module
handles a ``restored`` event it encounters rather than failing on it, because
section 8 requires the precheck to understand one, but handling it is not a
claim that calling undo twice is supported.
"""

from dataclasses import dataclass, replace
from uuid import UUID

from psycopg import errors as psycopg_errors

from . import domain, sql
from .errors import VersionConflictError
from .models import (
    EventOperation,
    Task,
    TaskEvent,
    UndoReason,
    UndoResult,
    UpdateTaskArgs,
)


# The six fields UPDATE_TASK_GUARDED can restore. A task snapshot carries all
# eleven columns, and UpdateTaskArgs forbids extra keys, so the projection is
# required rather than defensive. id, owner_id, version, created_at, and
# updated_at are not restorable by an update and are not meant to be: the first
# two never change, and the last three are the append-only record of the row
# having moved forward.
RESTORABLE_FIELDS = ("title", "notes", "due_date", "priority", "status", "blocked_by")


@dataclass(frozen=True, slots=True)
class _TaskState:
    """Whether a task exists and at which version, in one of two projections."""

    exists: bool
    version: int | None


def undo_run(run_id: UUID, actor_id: UUID) -> UndoResult:
    """Compensate one run's events in reverse, entirely or not at all.

    Returns rather than raises on a conflict, because a refusal is an answer the
    caller displays and not an error. Every refusal path rolls back first, so a
    refused undo leaves both surfaces untouched: no task row moved and no
    task_events row was written.

    The actor scopes every read and every write. A run belonging to another
    actor reaches no rows through the owner-scoped statements below and refuses
    the same way a run whose tasks were deleted refuses, which is the
    indistinguishability section 6 requires. Resolving the run itself against
    ``agent_runs`` belongs to the wire contract in T08, not here.
    """
    with _pool().connection() as conn:
        try:
            events = _load_events(run_id, conn=conn)
            # 2. Section 8. An empty run is not a refusal, it is nothing to do.
            if not events:
                conn.rollback()
                return UndoResult(applied=0, refused=False)

            current = _load_current_state(actor_id, events, conn=conn)
            diverged = _load_diverged_task_ids(events, conn=conn)

            # 3. PRECHECK PASS, no writes.
            refusal = _precheck(events, current, diverged)
            if refusal is not None:
                conn.rollback()
                return UndoResult(applied=0, refused=True, reason=refusal)

            # 4. APPLY PASS, single transaction, same reverse order.
            applied = _apply(run_id, actor_id, events, current, conn=conn)
            conn.commit()

            # 5.
            return UndoResult(applied=applied, refused=False)
        except VersionConflictError:
            # A guarded update or guarded delete touched zero rows, so the row
            # moved after the precheck read it. The precheck holds the finer
            # distinction between moved and gone; at this point both are the
            # same conflict.
            conn.rollback()
            return UndoResult(
                applied=0, refused=True, reason=UndoReason.VERSION_CONFLICT
            )
        except psycopg_errors.UniqueViolation:
            # The primary key on INSERT_TASK_RESTORED. The precheck established
            # that the id was absent, so the row reappeared in between. The
            # transaction is already aborted here, which is why this is caught
            # at the boundary and translated after the rollback rather than
            # handled in place: a savepoint would let the rest of the undo
            # proceed, and there is no correct undo that proceeds past a
            # conflict.
            conn.rollback()
            return UndoResult(applied=0, refused=True, reason=UndoReason.ROW_RECREATED)
        except psycopg_errors.ForeignKeyViolation:
            # A restored task points at a blocker that no longer exists. Reverse
            # order restores a blocker this run deleted before any pointer to
            # it, so this means the blocker was removed by someone else.
            conn.rollback()
            return UndoResult(
                applied=0, refused=True, reason=UndoReason.ROW_DISAPPEARED
            )


def _load_events(run_id: UUID, *, conn) -> list[TaskEvent]:
    """1. Load events for run_id, ordered by id DESCENDING.

    Unbounded on purpose. See the comment on SELECT_ALL_EVENTS_FOR_RUN: a
    truncated read here does not shorten the answer, it silently converts an
    all-or-nothing compensation into a partial one that reports success.
    """
    rows = conn.execute(sql.SELECT_ALL_EVENTS_FOR_RUN, {"run_id": run_id}).fetchall()
    return [TaskEvent.model_validate(row) for row in rows]


def _load_current_state(
    actor_id: UUID, events: list[TaskEvent], *, conn
) -> dict[UUID, Task]:
    """Load every task the run touched, owner scoped, in canonical id order.

    FOR UPDATE is a strengthening rather than the correctness condition. Rows
    absent from the result cannot be locked at all, so the guards on the
    compensating writes are what actually make the apply pass safe. Holding the
    locks means the common conflicts surface in the precheck, where they carry a
    precise reason, instead of at the write, where a guarded delete cannot tell
    a moved row from a missing one.
    """
    task_ids = sorted({event.task_id for event in events})
    rows = conn.execute(
        sql.SELECT_TASKS_BY_IDS_FOR_UPDATE,
        {"owner_id": actor_id, "task_ids": task_ids},
    ).fetchall()
    tasks = (Task.model_validate(row) for row in rows)
    return {task.id: task for task in tasks}


def _load_diverged_task_ids(events: list[TaskEvent], *, conn) -> set[UUID]:
    """Every task the run touched whose local integration state is diverged.

    T00L, under D-27. Deliberately NOT owner scoped and deliberately not joined
    to `tasks`, which is the whole reason `linear_task_state` carries no foreign
    key: the flag has to remain readable for a task this very run deleted, and a
    join or an owner scope would silently drop exactly that case and turn the
    tombstone refusal into a pass.

    Read on the caller's connection so it sits inside the same transaction and
    the same snapshot as the locked rows above. Undo reads this table and never
    writes it. No Linear call happens here or anywhere below.
    """
    task_ids = sorted({event.task_id for event in events})
    rows = conn.execute(
        sql.SELECT_DIVERGED_TASK_IDS, {"task_ids": task_ids}
    ).fetchall()
    return {row["task_id"] for row in rows}


def _precheck(
    events: list[TaskEvent],
    current: dict[UUID, Task],
    diverged: set[UUID],
) -> UndoReason | None:
    """3. Every event, before any of them is applied. Returns the refusal reason.

    Divergence is checked first and is not operation-specific: it is a statement
    about who else has touched the task, not about what this event did to it.

    The condition below is operation-specific, because "the row is gone" is a conflict
    for some operations and the expected state for others. A version check alone
    is not sufficient: if an outside actor deleted a task this run had created or
    updated, there is no row and therefore no version to compare, and a
    version-only precheck would pass and then apply against nothing.

    Comparison is against projected historical state, not against the database a
    second time. For the newest event on a task those are the same value. For an
    earlier one they are not, and the difference is load bearing in both
    directions: it stops a multi-touch run from refusing its own undo, and it
    catches a foreign write that landed between two of this run's own events on
    one task, where the newest event still agrees with the database and undoing
    the earlier one would destroy a change this run never made.
    """
    # T00L divergence, under D-27, as one pass over every affected task before
    # any operation-specific check runs.
    #
    # The Linear design states this per event, alongside the existing three
    # reasons. One pass ahead of the loop agrees with that wording on every run
    # where exactly one refusal applies, and differs only where two do: walking
    # the events would report whichever reason the newest event happens to
    # produce, so a run whose newest event has a stale version and whose oldest
    # touches a diverged task would refuse VERSION_CONFLICT and never mention
    # that an issue was edited in Linear. The refusal reason is displayed to a
    # human who then has to decide what to do, and "someone else changed this
    # outside the system" is the reason that changes their next action. Making
    # it win deterministically is a strengthening, and it refuses in strictly
    # the same set of cases.
    #
    # No write has happened at this point and none can: this is the precheck,
    # and every path out of it rolls back. Divergence therefore produces
    # applied = 0 with no compensation, no compensation task_event, no version
    # change, and no write to linear_task_state.
    if diverged:
        return UndoReason.EXTERNALLY_MODIFIED

    projected = {
        task_id: _TaskState(exists=True, version=task.version)
        for task_id, task in current.items()
    }
    for event in events:
        state = projected.get(event.task_id, _TaskState(exists=False, version=None))
        operation = _effective_operation(event)

        if operation is EventOperation.DELETED:
            # The row MUST still be absent.
            if state.exists:
                return UndoReason.ROW_RECREATED
        else:
            # created and updated: the row MUST exist at the version this event
            # left behind.
            if not state.exists:
                return UndoReason.ROW_DISAPPEARED
            if state.version != _event_version(event.after):
                return UndoReason.VERSION_CONFLICT

        projected[event.task_id] = _historical_state(event.before)

    return None


def _apply(
    run_id: UUID,
    actor_id: UUID,
    events: list[TaskEvent],
    current: dict[UUID, Task],
    *,
    conn,
) -> int:
    """4. Reverse order, one transaction, guarded at every write.

    The version passed to each guard is the physical one, tracked as the row
    actually moves, not the historical one in the snapshot. Undoing a delete
    re-inserts at the deleted version plus one, so the next compensation on that
    task expects that number and not the version the earlier event recorded.

    Each apply writes new task_events rows with run_id set to the ORIGINAL
    run_id. Undo never deletes or rewrites a task_events row; history is
    append-only.
    """
    physical = {
        task_id: _TaskState(exists=True, version=task.version)
        for task_id, task in current.items()
    }
    applied = 0

    for event in events:
        state = physical.get(event.task_id, _TaskState(exists=False, version=None))
        operation = _effective_operation(event)

        if operation is EventOperation.CREATED:
            # The event brought the row into existence. Remove it.
            mutation = domain.delete_task_guarded(
                actor_id, event.task_id, state.version, conn=conn
            )
            physical[event.task_id] = _TaskState(exists=False, version=None)
        elif operation is EventOperation.DELETED:
            # The event removed the row. Put it back under its original id, at
            # the deleted version plus one.
            restored_version = _event_version(event.before) + 1
            mutation = domain.restore_task(
                actor_id, event.before, version=restored_version, conn=conn
            )
            physical[event.task_id] = _TaskState(
                exists=True, version=restored_version
            )
        else:
            mutation = domain.update_task(
                actor_id, _restore_arguments(event, state.version), conn=conn
            )
            physical[event.task_id] = _TaskState(
                exists=True, version=state.version + 1
            )

        domain.write_events(
            run_id, actor_id, _compensation_events(event.task_id, mutation), conn=conn
        )
        applied += 1

    return applied


def _effective_operation(event: TaskEvent) -> EventOperation:
    """Section 8: treat 'restored' as the operation its snapshots describe.

    Section 8 says to treat a restored event as an update, which is right for
    the restored events this module writes when compensating an update. It also
    writes them when compensating a create or a delete, and those carry a null
    after or a null before respectively. Resolving by snapshot shape covers all
    three with no special case and leaves created, updated, and deleted
    untouched, because their shapes already agree with their names.
    """
    if event.operation is not EventOperation.RESTORED:
        return event.operation
    if event.before is None:
        return EventOperation.CREATED
    if event.after is None:
        return EventOperation.DELETED
    return EventOperation.UPDATED


def _historical_state(before) -> _TaskState:
    """The state an event found before it ran, from its own snapshot."""
    if before is None:
        return _TaskState(exists=False, version=None)
    return _TaskState(exists=True, version=_event_version(before))


def _event_version(snapshot) -> int:
    return int(snapshot["version"])


def _restore_arguments(event: TaskEvent, expected_version: int) -> UpdateTaskArgs:
    """Project the before snapshot onto the six fields an update can restore.

    Every one of the six is passed explicitly, including the ones whose value is
    None. T06's contract is that model_fields_set is the only signal separating
    an omitted nullable field from an explicit null, so a partial projection here
    would leave due_date or blocked_by at their post-mutation values while
    reporting a full restore.
    """
    before = event.before
    fields = {name: before[name] for name in RESTORABLE_FIELDS}
    return UpdateTaskArgs(
        task_id=event.task_id,
        expected_version=expected_version,
        **fields,
    )


def _compensation_events(task_id: UUID, mutation: domain.MutationResult):
    """Relabel the direct compensation to 'restored' and leave the cascade alone.

    D-41. The direct inverse of the run's event is the compensation, and section
    8 names it restored. The additional rows a compensating delete produces
    through ON DELETE SET NULL are fresh side effects rather than inverses of
    anything, so they keep the updated semantics D-23 gave them. Relabelling
    those too would make the event log claim a pointer was restored when it was
    cleared.
    """
    return tuple(
        replace(event, operation=EventOperation.RESTORED)
        if event.task_id == task_id
        else event
        for event in mutation.events
    )


def _pool():
    """Imported lazily, matching policy.py and idempotency.py.

    db.py opens its ConnectionPool at import time with open=True, so a
    module-level import would make importing this module require a live
    database.
    """
    from .db import pool

    return pool
