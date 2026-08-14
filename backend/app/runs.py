"""Run record lifecycle and server-owned history, from BUILD_SPEC section 9.

This module is the only place that resolves a run identifier to a run, and the
only place that reads or writes `agent_runs.message_history`. Section 9 states
the second rule directly: "A single function, `runs.load_history(run_id)`, is
the only source of history anywhere in the codebase; no other code path
constructs a message list from a request."

The first rule is what makes a browser-supplied run id safe to accept. `load`
resolves the id against `agent_runs` and refuses unless the row exists and
belongs to `actor_id`. Both failures raise the same `OutOfScopeError`, matching
the architectural invariant that a missing resource and another actor's resource
are indistinguishable from outside. `SELECT_RUN` carries both predicates, so a
caller cannot resolve a run without also asserting ownership.

`load_history` deliberately takes no `actor_id`, because section 9 fixes its
signature. Callers resolve first with `load` and then read history by id. Every
route in `main.py` does exactly that.

Approval reads live here rather than in a module of their own. Section 10's tool
body names `approvals.load(...)`, but section 3's file tree has no `approvals.py`
and no task creates one, while `INSERT_APPROVAL`, `SELECT_APPROVAL`, and
`DECIDE_APPROVAL` have sat in `sql.py` without a caller since T02. Approval rows
are run-scoped control state, so `runs.load_approval` is where they belong and
section 10 is amended to name it. See D-42.
"""

from datetime import timedelta
from uuid import UUID

from psycopg.types.json import Json

from app import sql
from app.config import settings
from app.db import pool
from app.errors import OutOfScopeError
from app.models import (
    AgentRun,
    Approval,
    EventOperation,
    RunDetail,
    RunStatus,
    RunStep,
    RunUsage,
    TaskEvent,
    ToolName,
    ToolStepStatus,
)


# A run is eligible to attempt undo only after it has stopped producing effects.
# D-44. `running` and `awaiting_approval` are excluded because either can still
# commit another tool call, and compensating a live run races its own
# continuation. `failed` and `interrupted` stay eligible because both can have
# committed tools before the run stopped.
UNDOABLE_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}
)


def create(actor_id: UUID, prompt: str, model: str) -> AgentRun:
    """Open a run. The server issues the id; no caller may supply one."""
    with pool.connection() as conn:
        row = conn.execute(
            sql.INSERT_RUN,
            {"actor_id": actor_id, "prompt": prompt, "model": model},
        ).fetchone()
        conn.commit()
    return AgentRun.model_validate(row)


def load(run_id: UUID, actor_id: UUID) -> AgentRun:
    """Resolve a run id to a run this actor owns, or refuse.

    This is the function that makes a browser-supplied identifier a lookup key
    rather than a grant. A run that does not exist and a run belonging to
    another actor both raise OutOfScopeError, so the response cannot be used to
    enumerate which run ids are real.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.SELECT_RUN, {"run_id": run_id, "actor_id": actor_id}
        ).fetchone()
        conn.commit()
    if row is None:
        raise OutOfScopeError()
    return AgentRun.model_validate(row)


def load_history(run_id: UUID, actor_id: UUID) -> list:
    """The single source of message history in the codebase. See section 9.

    Section 9 writes this as `runs.load_history(run_id)`. It takes `actor_id`
    as well, because history is server-owned run state and reading it means
    resolving the run first, which is an ownership question. D-15 set the
    precedent: section 10's `policy.check(...)` line likewise omitted arguments
    the function requires, and the signature was corrected rather than the
    check weakened. The property section 9 is protecting is that exactly one
    function reads history, and that is unaffected.

    Nothing else in the codebase constructs a message list, and no request model
    carries one.
    """
    return list(load(run_id, actor_id).message_history)


def save_history(run_id: UUID, message_history: list) -> None:
    """Replace the server-owned history for a run."""
    with pool.connection() as conn:
        conn.execute(
            sql.UPDATE_RUN_HISTORY,
            {"run_id": run_id, "message_history": Json(message_history)},
        )
        conn.commit()


def set_status(run_id: UUID, status: RunStatus, error: str | None = None) -> AgentRun:
    """Move a run's status. UPDATE_RUN_STATUS stamps ended_at on terminal moves."""
    with pool.connection() as conn:
        row = conn.execute(
            sql.UPDATE_RUN_STATUS,
            {"run_id": run_id, "status": status.value, "error": error},
        ).fetchone()
        conn.commit()
    return AgentRun.model_validate(row)


def record_usage(
    run_id: UUID,
    *,
    model_calls: int = 0,
    tool_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_cents: float = 0.0,
) -> AgentRun:
    """Accumulate usage. UPDATE_RUN_USAGE adds rather than replaces, because one
    application run can contain several model invocations. See section 10."""
    with pool.connection() as conn:
        row = conn.execute(
            sql.UPDATE_RUN_USAGE,
            {
                "run_id": run_id,
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_cents": cost_cents,
            },
        ).fetchone()
        conn.commit()
    return AgentRun.model_validate(row)


def load_approval(run_id: UUID, tool_call_id: str) -> Approval | None:
    """Load one approval row, or None when the tool is ungated.

    Section 10 step 2 calls this before `policy.check`, which takes the row as a
    parameter and never writes one. Creation and decision belong to T12B, which
    owns the approval bridge.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.SELECT_APPROVAL,
            {"run_id": run_id, "tool_call_id": tool_call_id},
        ).fetchone()
        conn.commit()
    return None if row is None else Approval.model_validate(row)


def detail(run_id: UUID, actor_id: UUID) -> RunDetail:
    """Assemble the section 9 RunDetail for one resolved run.

    `pending_approval` is null at T08 and is not a placeholder for a read that
    was forgotten. Nothing writes an approval row yet, and the question of what
    the field means when a model produces more than one approval-required call
    is genuinely unspecified. T12B owns both. See D-45.
    """
    run = load(run_id, actor_id)
    with pool.connection() as conn:
        invocations = conn.execute(
            sql.SELECT_INVOCATIONS_FOR_RUN, {"run_id": run_id}
        ).fetchall()
        events = conn.execute(
            sql.SELECT_ALL_EVENTS_FOR_RUN, {"run_id": run_id}
        ).fetchall()
        conn.commit()

    return RunDetail(
        id=run.id,
        status=run.status,
        prompt=run.prompt,
        pending_approval=None,
        steps=[_step(row) for row in invocations],
        usage=RunUsage(
            model_calls=run.model_calls,
            tool_calls=run.tool_calls,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cost_cents=float(run.cost_cents),
        ),
        can_undo=_can_undo(run, [TaskEvent.model_validate(row) for row in events]),
        error=run.error,
    )


def _can_undo(run: AgentRun, events: list[TaskEvent]) -> bool:
    """D-44. Eligibility to attempt undo, not a promise that undo will succeed.

    The precheck in `undo.py` can still refuse with ROW_DISAPPEARED,
    VERSION_CONFLICT, or ROW_RECREATED when current state has moved underneath
    the run. This predicate answers only whether an attempt is well defined.

    The third clause is what D-38 requires. Undo is a single application to an
    eligible run, and compensation events carry the original run_id, so a second
    call would load the original wave and its own compensations together and
    undo a combined history that inverts nothing. `undo.py` processes a restored
    event rather than refusing it, by design, so this is the only thing standing
    between the demo and a second undo. T18 enforces the same predicate before
    calling the kernel.
    """
    if run.status not in UNDOABLE_STATUSES:
        return False
    if not events:
        return False
    return not any(
        event.operation is EventOperation.RESTORED for event in events
    )


def _step(row) -> RunStep:
    """One durable tool_invocations row as a section 9 RunStep.

    `deduplicated` is structurally unreachable here, and that is a property of
    the persistence model rather than an omission. `idempotency.acquire` returns
    REPLAY straight off a read of a completed row and writes nothing, so a
    successful replay leaves no trace to render. Every path that does rewrite
    the row, failed reacquisition and expired-lease theft, increments `attempt`,
    which is why `attempt` below is truthful. See D-43.
    """
    status = ToolStepStatus(row["status"])
    return RunStep(
        tool_call_id=row["tool_call_id"],
        tool_name=ToolName(row["tool_name"]),
        attempt=row["attempt"],
        status=status,
        duration_ms=_duration_ms(row, status),
        error=row["error"],
    )


def _duration_ms(row, status: ToolStepStatus) -> int:
    """Elapsed time of the most recent persisted attempt. D-43.

    `created_at` is the wrong anchor. Neither REACQUIRE_FAILED_LEASE nor
    STEAL_EXPIRED_LEASE rewrites it, so measuring from it would charge a stolen
    lease for the dead holder's entire expiry window and report a three second
    tool as two minutes. Both statements do set `lease_expires_at` to
    `now() + ttl`, and so does INSERT_LEASE, so subtracting the configured TTL
    recovers the moment this attempt was granted.

    The branch is on `status`, not on `completed_at IS NULL`, and that is
    load bearing. REACQUIRE_FAILED_LEASE returns a row to `pending` and bumps
    `attempt` without clearing `completed_at`, so a reacquired row is pending
    while still carrying the previous attempt's completion stamp. Branching on
    the timestamp would take the terminal path, subtract a later anchor from an
    earlier stamp, and report a negative duration.

    Limitation: the reconstruction assumes LEASE_TTL_SECONDS has not changed
    since this attempt acquired its lease. Changing the TTL while historical
    rows remain makes the derived start inaccurate for those rows, which is why
    the result is clamped at zero rather than allowed to go negative.
    """
    started_at = row["lease_expires_at"] - timedelta(
        seconds=settings.lease_ttl_seconds
    )
    observed_at = row["observed_at"] if status is ToolStepStatus.PENDING else row["completed_at"]
    if observed_at is None:
        return 0
    return max(0, int((observed_at - started_at).total_seconds() * 1000))
