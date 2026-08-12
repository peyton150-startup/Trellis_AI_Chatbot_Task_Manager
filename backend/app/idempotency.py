"""KERNEL. Lease acquire, complete, and replay.

Transcribed from BUILD_SPEC section 7. Lease states and the transaction boundary
are the reliability demo, and getting them nearly right produces a demo that
lies, so nothing here is reordered, collapsed, or improved.

The property the whole module exists to hold is narrow: only the caller whose
guarded UPDATE touches a row may execute. Every branch that could hand out the
right to execute does so through a statement whose guard is evaluated server
side, inside the UPDATE. A SELECT that observes the state followed by an
unguarded write is not equivalent, because two retries can both observe the same
row and both proceed. Section 7 calls that the bug rather than a style
preference.

D-18 adds the caller's connection to complete as a required keyword argument.
Section 7 calls it non-negotiable that the domain mutation, its task_events
rows, and complete share one transaction, while the signature printed there
passes no connection and db.py hands out a distinct pooled connection per
pool.connection() call. As printed, complete would commit separately, which is
exactly the window section 7 forbids. acquire and fail keep their printed
signatures and open their own connections, because acquire must commit
independently or a concurrent retry never conflicts on INSERT_LEASE, and fail
runs after the domain transaction has already rolled back.

D-19 covers the two guarded statements, REACQUIRE_FAILED_LEASE and
STEAL_EXPIRED_LEASE.
"""

import time
from datetime import datetime, timezone
from uuid import UUID

from psycopg.types.json import Json

from . import sql
from .config import settings
from .errors import IdempotencyConflictError, LeaseInFlightError
from .models import LeaseAction, LeaseOutcome, LeaseStatus


# Section 7: re-SELECT every 250ms, up to 8 times, 2s total.
POLL_INTERVAL_SECONDS = 0.25
POLL_ATTEMPTS = 8

# A guarded UPDATE that touches zero rows means another retry won, and section 7
# says to re-SELECT and switch on the new status. That is a loop, and this
# bounds it. Each pass requires a competing retry to have changed the row since
# the previous SELECT, so reaching the bound means contention that polling
# cannot resolve either, and LEASE_IN_FLIGHT is the honest answer.
RESOLVE_ATTEMPTS = 8


def acquire(
    run_id: UUID,
    tool_call_id: str,
    tool_name: str,
    args_hash: str,
) -> LeaseOutcome:
    """Take the lease for one tool call, or discover who already holds it.

    Returns EXECUTE when this caller owns the right to run the tool body, and
    REPLAY with the stored result when the work already committed. Raises rather
    than returning anything ambiguous: a null result is never returned for a
    pending row, and a hash mismatch is never treated as a replay.
    """
    with _pool().connection() as conn:
        # 1. Execute INSERT_LEASE. ON CONFLICT DO NOTHING is what makes it a
        #    lease: exactly one concurrent caller gets a row back.
        inserted = conn.execute(
            sql.INSERT_LEASE,
            {
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments_hash": args_hash,
                "lease_ttl_seconds": settings.lease_ttl_seconds,
            },
        ).fetchone()
        # Committed here, not at the end of the tool body. A lease no concurrent
        # retry can observe is not a lease.
        conn.commit()

        # 2.
        if inserted is not None:
            return LeaseOutcome(action=LeaseAction.EXECUTE)

        # 3. It conflicted.
        return _resolve_conflict(conn, run_id, tool_call_id, args_hash)


def complete(run_id: UUID, tool_call_id: str, result, *, conn) -> None:
    """Mark the lease completed and store the result the retry will replay.

    conn is required and keyword-only under D-18. This statement must run inside
    the caller's open transaction, alongside the domain mutation and its
    task_events rows, so that all three commit or roll back together. If the
    process dies before that commit, nothing happened and the row stays pending.
    If it dies after, the lease says completed and the retry replays. There is
    no window where the mutation committed and the lease did not.

    It deliberately does not commit. The caller owns the transaction, and
    committing here would end it early and reintroduce the window.
    """
    conn.execute(
        sql.COMPLETE_LEASE,
        {
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "result": Json(result),
        },
    )


def fail(run_id: UUID, tool_call_id: str, error: str) -> None:
    """Mark the lease failed after the domain transaction rolled back.

    This runs on its own connection by necessity: the transaction complete would
    have joined no longer exists, and writing through it is impossible. A failed
    row is not a terminal state. The next retry reacquires it through the guard
    in REACQUIRE_FAILED_LEASE.
    """
    with _pool().connection() as conn:
        conn.execute(
            sql.FAIL_LEASE,
            {
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "error": error,
            },
        )
        conn.commit()


def _resolve_conflict(conn, run_id, tool_call_id, args_hash) -> LeaseOutcome:
    """Steps 3 through 5. The row exists; decide what this caller may do."""
    for _ in range(RESOLVE_ATTEMPTS):
        existing = _select_lease(conn, run_id, tool_call_id)
        if existing is None:
            # The row was there for the ON CONFLICT and is gone now. Only a run
            # cascade deletes tool_invocations, so the run itself was removed
            # underneath this call. Nothing may execute against it.
            raise LeaseInFlightError()

        # 4. Ahead of the switch on status, deliberately. A completed row whose
        #    hash differs would otherwise replay a result belonging to different
        #    arguments, which is the one outcome worse than re-executing.
        if existing["arguments_hash"] != args_hash:
            raise IdempotencyConflictError()

        status = existing["status"]

        # 5. completed.
        if status == LeaseStatus.COMPLETED.value:
            return LeaseOutcome(action=LeaseAction.REPLAY, result=existing["result"])

        # 5. failed. Do not return EXECUTE from the observed status. Two retries
        #    can both observe failed and both execute, so reacquire atomically
        #    and let the UPDATE decide which one wins.
        if status == LeaseStatus.FAILED.value:
            if _reacquire_failed(conn, run_id, tool_call_id) is not None:
                return LeaseOutcome(action=LeaseAction.EXECUTE)
            # Zero rows: another retry won. Re-SELECT and switch on the new
            # status rather than guessing what it became.
            continue

        # 5. pending.
        if existing["lease_expires_at"] < _now():
            # The holder died. Steal it, with the expiry guard in the UPDATE so
            # that only one of several racing thieves can succeed.
            if _steal_expired(conn, run_id, tool_call_id) is not None:
                return LeaseOutcome(action=LeaseAction.EXECUTE)
            # Zero rows: someone else stole it and now holds a live lease. Poll.

        polled = _poll(conn, run_id, tool_call_id)
        if polled is not None:
            return polled
        # The poll saw the row become failed. Reacquiring it is the failed
        # branch above, not a direct EXECUTE, so go back through the switch.

    raise LeaseInFlightError()


def _poll(conn, run_id, tool_call_id) -> LeaseOutcome | None:
    """Re-SELECT every 250ms, up to 8 times.

    Returns REPLAY if the holder committed, None if the row became failed so the
    caller can take the guarded reacquire path, and raises LEASE_IN_FLIGHT if it
    is still pending when the polls run out. It never returns a null result for
    a pending row.
    """
    for _ in range(POLL_ATTEMPTS):
        time.sleep(POLL_INTERVAL_SECONDS)
        current = _select_lease(conn, run_id, tool_call_id)
        if current is None:
            raise LeaseInFlightError()
        if current["status"] == LeaseStatus.COMPLETED.value:
            return LeaseOutcome(action=LeaseAction.REPLAY, result=current["result"])
        if current["status"] == LeaseStatus.FAILED.value:
            return None
    raise LeaseInFlightError()


def _select_lease(conn, run_id, tool_call_id):
    row = conn.execute(
        sql.SELECT_LEASE, {"run_id": run_id, "tool_call_id": tool_call_id}
    ).fetchone()
    conn.commit()
    return row


def _reacquire_failed(conn, run_id, tool_call_id):
    row = conn.execute(
        sql.REACQUIRE_FAILED_LEASE,
        {
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "lease_ttl_seconds": settings.lease_ttl_seconds,
        },
    ).fetchone()
    conn.commit()
    return row


def _steal_expired(conn, run_id, tool_call_id):
    row = conn.execute(
        sql.STEAL_EXPIRED_LEASE,
        {
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "lease_ttl_seconds": settings.lease_ttl_seconds,
        },
    ).fetchone()
    conn.commit()
    return row


def _pool():
    """Imported lazily, matching policy.py.

    db.py opens its ConnectionPool at import time with open=True, so a
    module-level import would make importing this module require a live
    database. tools.py imports the kernel to build every tool body.
    """
    from .db import pool

    return pool


def _now() -> datetime:
    return datetime.now(timezone.utc)
