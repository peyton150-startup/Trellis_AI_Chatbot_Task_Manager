"""The thirteen deterministic invariant tests from BUILD_SPEC section 11.

T04 owns six of them and T05 adds three more. The remaining four belong to T07
and T08 and are deliberately absent rather than skipped, so the count of
collected tests always matches the count of proven invariants.

None of these constructs an Agent, calls a model, or makes a network call. They
call the policy layer and the idempotency kernel directly against real
PostgreSQL. For policy that is because check reads task ownership from the
database and a faked lookup would prove only the fake. For idempotency it is
because leases are database rows and every guard section 7 specifies lives
inside an UPDATE statement, so a faked lease would prove nothing about the one
property under test: that only the caller whose guarded UPDATE touches a row may
execute.

Fixture values are round and hand-checkable per section 11: no randomness, no
faker, and no dependence on the current time except the two expiry cases, both
of which are anchored by a negative TTL against the database clock rather than
by injecting a clock into the kernel. See D-15, which fixes that technique for
the approval row and states that it covers lease_expires_at unchanged.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from psycopg.types.json import Json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import idempotency, policy, sql
from app.config import settings
from app.db import pool
from app.errors import PolicyError
from app.models import Approval, LeaseAction, LeaseStatus, ToolInvocation, ToolName


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")
MISSING_TASK_ID = UUID("00000000-0000-0000-0000-0000000000ff")

TOOL_CALL_ID = "call-t04-0001"
OTHER_TOOL_CALL_ID = "call-t04-0002"

LEASE_CALL_ID = "call-t05-0001"
OTHER_LEASE_CALL_ID = "call-t05-0002"

TODAY = date(2026, 8, 17)

# INSERT_APPROVAL computes expires_at as now() + make_interval(secs => ttl), so a
# negative TTL produces a row that is already expired against the database clock.
# INSERT_LEASE computes lease_expires_at the same way, so the identical technique
# produces the dead holder the theft test needs. See D-15.
VALID_TTL_SECONDS = 300
EXPIRED_TTL_SECONDS = -3600

# ApprovalPreview forbids extra keys, so a stored preview carries exactly these.
EMPTY_PREVIEW = {"creates": [], "updates": [], "deletes": []}

# A known answer for the one hash definition in the codebase, so a change to its
# canonicalization cannot pass unnoticed just because the fixture and the kernel
# compute it the same wrong way. sha256 of
# {"priority":"high","task_ids":["b","a"]} with sorted keys and no whitespace.
HASH_SAMPLE = {"task_ids": ["b", "a"], "priority": "high"}
HASH_SAMPLE_REORDERED = {"priority": "high", "task_ids": ["b", "a"]}
HASH_SAMPLE_DIGEST = "88d2f0a2278f2b0a093d457704683a4f92c8b3a8c5d2d4422b1a2f34e90d15a1"


@pytest.fixture
def db():
    """A committed connection against a state-free database.

    policy.check opens its own connection from the pool, so fixture writes must
    be committed rather than held open in the test's transaction.
    """
    with pool.connection() as conn:
        _truncate(conn)
        try:
            yield conn
        finally:
            _truncate(conn)


def _truncate(conn):
    conn.execute(sql.TRUNCATE_ALL_STATE)
    conn.commit()


def _insert_run(conn, actor_id=ACTOR_ID):
    row = conn.execute(
        sql.INSERT_RUN,
        {
            "actor_id": actor_id,
            "prompt": "T04 invariant fixture",
            "model": "t04-fixture-model",
        },
    ).fetchone()
    conn.commit()
    return row["id"]


def _insert_task(conn, owner_id, title):
    row = conn.execute(
        sql.INSERT_TASK,
        {
            "owner_id": owner_id,
            "title": title,
            "notes": "",
            "due_date": TODAY,
            "priority": "medium",
            "blocked_by": None,
        },
    ).fetchone()
    conn.commit()
    return row["id"]


def _insert_approval(
    conn,
    run_id,
    tool_call_id,
    arguments,
    ttl_seconds=VALID_TTL_SECONDS,
    decision="approved",
):
    """Insert an approval row and optionally decide it.

    Every field except the one under test is left valid, so each rejection test
    isolates a single failing condition. Pass decision=None to leave the row in
    its inserted 'pending' state, which is what INSERT_APPROVAL defaults to.
    """
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
            "approval_ttl_seconds": ttl_seconds,
        },
    )
    if decision is not None:
        _decide(conn, run_id, tool_call_id, decision)
    conn.commit()
    return _load_approval(conn, run_id, tool_call_id)


def _decide(conn, run_id, tool_call_id, decision):
    """DECIDE_APPROVAL guards decision = 'pending', so this only moves a row once."""
    conn.execute(
        sql.DECIDE_APPROVAL,
        {"run_id": run_id, "tool_call_id": tool_call_id, "decision": decision},
    )
    conn.commit()
    return _load_approval(conn, run_id, tool_call_id)


def _insert_approved(conn, run_id, tool_call_id, arguments, ttl_seconds=VALID_TTL_SECONDS):
    return _insert_approval(conn, run_id, tool_call_id, arguments, ttl_seconds, "approved")


def _load_approval(conn, run_id, tool_call_id):
    row = conn.execute(
        sql.SELECT_APPROVAL, {"run_id": run_id, "tool_call_id": tool_call_id}
    ).fetchone()
    return Approval.model_validate(row)


def _delete_arguments(task_ids):
    return {"task_ids": [str(task_id) for task_id in task_ids]}


def _insert_lease(conn, run_id, tool_call_id, args_hash, ttl_seconds=VALID_TTL_SECONDS):
    """Write a lease row directly, bypassing acquire.

    Only the theft test uses this, to stand up a dead holder: a pending lease
    whose expiry is already in the past. A negative TTL produces that against
    the database clock, the same way the expired approval row does. Nothing here
    injects a clock into the kernel, and the theft guard is evaluated server
    side inside the UPDATE regardless.
    """
    conn.execute(
        sql.INSERT_LEASE,
        {
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "tool_name": ToolName.CREATE_TASK.value,
            "arguments_hash": args_hash,
            "lease_ttl_seconds": ttl_seconds,
        },
    )
    conn.commit()
    return _load_lease(conn, run_id, tool_call_id)


def _load_lease(conn, run_id, tool_call_id):
    row = conn.execute(
        sql.SELECT_LEASE, {"run_id": run_id, "tool_call_id": tool_call_id}
    ).fetchone()
    return None if row is None else ToolInvocation.model_validate(row)


def _commit_work(conn, run_id, tool_call_id, title):
    """The section 10 step 4 transaction, in full.

    The domain mutation, its task_events row, and complete() are one
    transaction. Under D-18 complete() takes the caller's connection as a
    required keyword argument precisely so this holds: were it to open its own
    pooled connection it would commit separately, which is the window section 7
    says must not exist.
    """
    task_row = conn.execute(
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
    conn.execute(
        sql.INSERT_TASK_EVENT,
        {
            "task_id": task_row["id"],
            "run_id": run_id,
            "actor_id": ACTOR_ID,
            "operation": "created",
            "before": None,
            "after": Json({"id": str(task_row["id"]), "title": title, "version": 1}),
        },
    )
    result = {"task_id": str(task_row["id"])}
    idempotency.complete(run_id, tool_call_id, result, conn=conn)
    conn.commit()
    return task_row["id"], result


def _count_tasks(conn):
    rows = conn.execute(
        sql.SELECT_TASKS_FOR_OWNER,
        {
            "owner_id": ACTOR_ID,
            "status": None,
            "due_before": None,
            "due_after": None,
            "priority": None,
            "limit": 50,
        },
    ).fetchall()
    return len(rows)


def _count_events(conn, run_id):
    rows = conn.execute(
        sql.SELECT_EVENTS_FOR_RUN, {"run_id": run_id, "limit": 50}
    ).fetchall()
    return len(rows)


def test_cross_actor_mutation_rejected(db):
    """Scope is step 1, ahead of the approval gate at steps 2 through 4.

    delete_tasks with no approval row would raise APPROVAL_REQUIRED if the gate
    ran first. OUT_OF_SCOPE proves scope wins, which is the ordering section 6
    says leaks the existence of other actors' rows when reversed.
    """
    foreign_task_id = _insert_task(db, OTHER_ACTOR_ID, "Task A")
    run_id = _insert_run(db)

    with pytest.raises(PolicyError) as foreign:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            _delete_arguments([foreign_task_id]),
            [foreign_task_id],
            None,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert foreign.value.code == "OUT_OF_SCOPE"
    assert foreign.value.http_status == 403

    with pytest.raises(PolicyError) as missing:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            _delete_arguments([MISSING_TASK_ID]),
            [MISSING_TASK_ID],
            None,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )

    # Section 6: missing and not-yours are not distinguished.
    assert missing.value.code == foreign.value.code
    assert missing.value.http_status == foreign.value.http_status


def test_forged_approval_rejected(db):
    """Three shapes of a client claiming an approval the server never granted.

    Scenarios 2 and 3 are what D-15's run_id and tool_call_id keyword arguments
    exist for: without them step 5a cannot be evaluated at all.
    """
    task_id = _insert_task(db, ACTOR_ID, "Task B")
    run_id = _insert_run(db)
    other_run_id = _insert_run(db)
    arguments = _delete_arguments([task_id])

    with pytest.raises(PolicyError) as no_row:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            arguments,
            [task_id],
            None,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert no_row.value.code == "APPROVAL_REQUIRED"
    assert no_row.value.http_status == 202

    # Same run, different call. Valid, approved, unexpired, correct hash.
    wrong_call = _insert_approved(db, run_id, OTHER_TOOL_CALL_ID, arguments)
    with pytest.raises(PolicyError) as call_mismatch:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            arguments,
            [task_id],
            wrong_call,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert call_mismatch.value.code == "APPROVAL_NOT_FOUND"
    assert call_mismatch.value.http_status == 403

    # Different run, same call id. approvals.run_id is a foreign key, hence the
    # second agent_runs row.
    wrong_run = _insert_approved(db, other_run_id, TOOL_CALL_ID, arguments)
    with pytest.raises(PolicyError) as run_mismatch:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            arguments,
            [task_id],
            wrong_run,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert run_mismatch.value.code == "APPROVAL_NOT_FOUND"
    assert run_mismatch.value.http_status == 403

    # Positive control. Without it every assertion above is one-sided, and a
    # check that rejected every non-null approval row would pass this test
    # completely while breaking the approval bridge. This is the only place the
    # six tests exercise section 6 step 6.
    genuine = _insert_approved(db, run_id, TOOL_CALL_ID, arguments)
    decision = policy.check(
        ACTOR_ID,
        ToolName.DELETE_TASKS,
        arguments,
        [task_id],
        genuine,
        run_id=run_id,
        tool_call_id=TOOL_CALL_ID,
    )
    assert decision.allow is True
    assert decision.approval_required is True
    assert decision.reason.value == "destructive"


def test_approval_hash_mismatch_rejected(db):
    """The row is real and matches the call. The arguments moved underneath it."""
    # Pin the canonicalization first. The fixture and check both compute the
    # stored hash with this same function, so they would agree even if it were
    # wrong: dropping sort_keys would leave every assertion below passing while
    # silently breaking the approval bridge and the T05 lease identity, which
    # section 6 makes this the single definition of.
    assert policy.arguments_hash(HASH_SAMPLE) == HASH_SAMPLE_DIGEST
    assert policy.arguments_hash(HASH_SAMPLE_REORDERED) == HASH_SAMPLE_DIGEST
    # Key order must not matter. Element order within a value must.
    assert policy.arguments_hash({"task_ids": ["a", "b"]}) != policy.arguments_hash(
        {"task_ids": ["b", "a"]}
    )

    first_task_id = _insert_task(db, ACTOR_ID, "Task C")
    second_task_id = _insert_task(db, ACTOR_ID, "Task D")
    run_id = _insert_run(db)

    approved_arguments = _delete_arguments([first_task_id])
    executed_arguments = _delete_arguments([first_task_id, second_task_id])

    approval_row = _insert_approved(db, run_id, TOOL_CALL_ID, approved_arguments)
    assert approval_row.arguments_hash == policy.arguments_hash(approved_arguments)
    assert approval_row.arguments_hash != policy.arguments_hash(executed_arguments)

    with pytest.raises(PolicyError) as mismatch:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            executed_arguments,
            [first_task_id, second_task_id],
            approval_row,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert mismatch.value.code == "APPROVAL_MISMATCH"
    assert mismatch.value.http_status == 403


def test_expired_approval_rejected(db):
    """Expiry is the only failing condition: identifiers, hash, and decision all valid."""
    task_id = _insert_task(db, ACTOR_ID, "Task E")
    run_id = _insert_run(db)
    arguments = _delete_arguments([task_id])

    approval_row = _insert_approved(
        db, run_id, TOOL_CALL_ID, arguments, ttl_seconds=EXPIRED_TTL_SECONDS
    )

    # Prove the fixture actually produced an expired row. Without this the test
    # could pass because the approval was never written at all.
    assert approval_row.expires_at < datetime.now(timezone.utc)
    assert approval_row.decision.value == "approved"
    assert approval_row.arguments_hash == policy.arguments_hash(arguments)

    with pytest.raises(PolicyError) as expired:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            arguments,
            [task_id],
            approval_row,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert expired.value.code == "APPROVAL_EXPIRED"
    assert expired.value.http_status == 403


def test_delete_without_approval_impossible(db):
    """Destructive ignores count. One target, far under the threshold, still gates.

    Also covers both branches of section 6 step 5d, because an approval row that
    exists but was never granted is still not approval. Neither branch is on the
    production path: under D-12 step 0 raises before check is reached, and the
    server persists the decision before building a continuation. They guard
    against retries, races, bypasses, and incorrect continuation sequencing.
    """
    task_id = _insert_task(db, ACTOR_ID, "Task F")
    run_id = _insert_run(db)
    arguments = _delete_arguments([task_id])

    assert 1 <= settings.blast_radius_threshold

    requirement = policy.classify(ToolName.DELETE_TASKS, arguments, 1)
    assert requirement.required is True
    assert requirement.reason.value == "destructive"

    with pytest.raises(PolicyError) as ungated:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            arguments,
            [task_id],
            None,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert ungated.value.code == "APPROVAL_REQUIRED"
    assert ungated.value.http_status == 202

    # Step 5d, pending. Identifiers, hash, and expiry are all valid, so the
    # undecided state is the only thing left to reject on.
    pending_row = _insert_approval(db, run_id, TOOL_CALL_ID, arguments, decision=None)
    assert pending_row.decision.value == "pending"

    with pytest.raises(PolicyError) as undecided:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            arguments,
            [task_id],
            pending_row,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert undecided.value.code == "APPROVAL_REQUIRED"
    assert undecided.value.http_status == 202

    # Step 5d, denied. The same row, decided against. A refusal is not a grant,
    # and it reports a different code from the undecided case above.
    denied_row = _decide(db, run_id, TOOL_CALL_ID, "denied")
    assert denied_row.decision.value == "denied"

    with pytest.raises(PolicyError) as refused:
        policy.check(
            ACTOR_ID,
            ToolName.DELETE_TASKS,
            arguments,
            [task_id],
            denied_row,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert refused.value.code == "APPROVAL_ALREADY_DECIDED"
    assert refused.value.http_status == 409


def test_bulk_over_threshold_requires_approval(db):
    """The boundary, explicitly: 3 does not require approval, 4 does."""
    assert settings.blast_radius_threshold == 3

    task_ids = [
        _insert_task(db, ACTOR_ID, title) for title in ("Task G", "Task H", "Task I", "Task J")
    ]
    run_id = _insert_run(db)

    at_threshold = task_ids[:3]
    over_threshold = task_ids[:4]

    at_arguments = {"task_ids": [str(task_id) for task_id in at_threshold]}
    over_arguments = {"task_ids": [str(task_id) for task_id in over_threshold]}

    at_requirement = policy.classify(ToolName.BULK_UPDATE_TASKS, at_arguments, 3)
    assert at_requirement.required is False

    over_requirement = policy.classify(ToolName.BULK_UPDATE_TASKS, over_arguments, 4)
    assert over_requirement.required is True
    assert over_requirement.reason.value == "blast_radius"

    decision = policy.check(
        ACTOR_ID,
        ToolName.BULK_UPDATE_TASKS,
        at_arguments,
        at_threshold,
        None,
        run_id=run_id,
        tool_call_id=TOOL_CALL_ID,
    )
    assert decision.allow is True
    assert decision.approval_required is False

    with pytest.raises(PolicyError) as over:
        policy.check(
            ACTOR_ID,
            ToolName.BULK_UPDATE_TASKS,
            over_arguments,
            over_threshold,
            None,
            run_id=run_id,
            tool_call_id=TOOL_CALL_ID,
        )
    assert over.value.code == "APPROVAL_REQUIRED"
    assert over.value.http_status == 202


def test_duplicate_tool_call_commits_once(db):
    """The lost-response retry, which D-04 calls the signature reliability moment.

    Two scenarios. The first is the retry itself: the same key replays the
    stored result and touches nothing. The second is the transaction boundary
    section 7 calls non-negotiable, because a replay that returns the right
    answer is worthless if the mutation and the lease could ever disagree.
    """
    run_id = _insert_run(db)
    arguments = {"title": "Task L"}
    args_hash = policy.arguments_hash(arguments)

    first = idempotency.acquire(run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, args_hash)
    assert first.action is LeaseAction.EXECUTE
    # Section 7: never return a null result for a pending row. EXECUTE carries
    # no result by construction, and the caller must not read one from it.
    assert first.result is None

    task_id, result = _commit_work(db, run_id, LEASE_CALL_ID, "Task L")

    second = idempotency.acquire(run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, args_hash)
    assert second.action is LeaseAction.REPLAY
    assert second.result == result
    assert second.result == {"task_id": str(task_id)}

    # The replay committed nothing. This is the actual invariant: one tool call
    # id, one mutation, regardless of how many times the caller retries.
    assert _count_tasks(db) == 1
    assert _count_events(db, run_id) == 1

    replayed_lease = _load_lease(db, run_id, LEASE_CALL_ID)
    assert replayed_lease.status is LeaseStatus.COMPLETED
    # A replay is not an attempt. Section 7 increments attempt only on a
    # reacquire or a steal, neither of which happened here.
    assert replayed_lease.attempt == 1

    # Scenario 2, the transaction boundary. The same three statements as
    # _commit_work, rolled back instead of committed, standing in for a process
    # that died before commit. Section 7: if it dies before commit, nothing
    # happened.
    rollback = idempotency.acquire(
        run_id, OTHER_LEASE_CALL_ID, ToolName.CREATE_TASK, args_hash
    )
    assert rollback.action is LeaseAction.EXECUTE

    db.execute(
        sql.INSERT_TASK,
        {
            "owner_id": ACTOR_ID,
            "title": "Task M",
            "notes": "",
            "due_date": TODAY,
            "priority": "medium",
            "blocked_by": None,
        },
    )
    idempotency.complete(
        run_id, OTHER_LEASE_CALL_ID, {"task_id": "rolled-back"}, conn=db
    )
    db.rollback()

    # The mutation did not land, and neither did the lease completion. There is
    # no window where one committed without the other.
    assert _count_tasks(db) == 1
    abandoned = _load_lease(db, run_id, OTHER_LEASE_CALL_ID)
    assert abandoned.status is LeaseStatus.PENDING
    assert abandoned.result is None

    # The lease row itself survived the rollback, because acquire commits
    # independently. It has to: a lease no concurrent retry can observe is not a
    # lease, and INSERT_LEASE's ON CONFLICT DO NOTHING is what makes it one.
    assert abandoned.tool_call_id == OTHER_LEASE_CALL_ID


def test_reused_key_different_args_conflicts(db):
    """Same key, different arguments. Section 7 step 4, ahead of the status switch.

    The ordering is the point. A hash mismatch is never a replay, so the check
    cannot sit after the switch on status, where a completed row would return
    the stored result for arguments nobody ever approved or executed.
    """
    run_id = _insert_run(db)
    original = {"title": "Task N"}
    tampered = {"title": "Task O"}
    original_hash = policy.arguments_hash(original)
    tampered_hash = policy.arguments_hash(tampered)
    assert original_hash != tampered_hash

    first = idempotency.acquire(run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, original_hash)
    assert first.action is LeaseAction.EXECUTE

    # Against a pending row. If the hash check sat after the status switch this
    # would poll for two seconds and raise LEASE_IN_FLIGHT instead, so this
    # assertion pins the order and not merely the code.
    with pytest.raises(PolicyError) as pending_conflict:
        idempotency.acquire(run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, tampered_hash)
    assert pending_conflict.value.code == "IDEMPOTENCY_CONFLICT"
    assert pending_conflict.value.http_status == 409

    task_id, result = _commit_work(db, run_id, LEASE_CALL_ID, "Task N")

    # Against a completed row, which is where treating a mismatch as a replay
    # would actually do damage: it would hand back another call's result.
    with pytest.raises(PolicyError) as completed_conflict:
        idempotency.acquire(run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, tampered_hash)
    assert completed_conflict.value.code == "IDEMPOTENCY_CONFLICT"
    assert completed_conflict.value.http_status == 409

    # Positive control. The conflict is about the arguments changing, not about
    # the key having been used before. Without this, an acquire that raised
    # IDEMPOTENCY_CONFLICT on every conflicting insert would pass everything
    # above while destroying the replay path the previous test proves.
    replay = idempotency.acquire(run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, original_hash)
    assert replay.action is LeaseAction.REPLAY
    assert replay.result == result

    # Nothing above committed a second mutation.
    assert _count_tasks(db) == 1
    assert _count_events(db, run_id) == 1
    assert str(task_id) == result["task_id"]


def test_expired_pending_lease_is_stolen(db):
    """A dead holder's expired lease is stolen and the work re-executes once.

    Section 14 lists this as correct rather than a bug, and section 7 explains
    why it is safe: the mutation, its events, and complete() share one
    transaction, so a pending row means the transaction never committed and the
    stolen work left no trace.
    """
    run_id = _insert_run(db)
    arguments = {"title": "Task P"}
    args_hash = policy.arguments_hash(arguments)

    # The dead holder. It acquired a lease and died before committing anything,
    # so there is a pending row, no task, and no event.
    dead = _insert_lease(db, run_id, LEASE_CALL_ID, args_hash, ttl_seconds=EXPIRED_TTL_SECONDS)
    assert dead.status is LeaseStatus.PENDING
    assert dead.attempt == 1
    # Prove the fixture really produced an expired lease. Without this the test
    # could pass because the row was never written the way it claims.
    assert dead.lease_expires_at < datetime.now(timezone.utc)
    assert _count_tasks(db) == 0

    stolen = idempotency.acquire(run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, args_hash)
    assert stolen.action is LeaseAction.EXECUTE

    after_steal = _load_lease(db, run_id, LEASE_CALL_ID)
    assert after_steal.status is LeaseStatus.PENDING
    # The steal took the lease rather than merely reading it: attempt moved, and
    # the new expiry is live, so the thief now holds it against anyone else.
    assert after_steal.attempt == 2
    assert after_steal.lease_expires_at > datetime.now(timezone.utc)

    task_id, result = _commit_work(db, run_id, LEASE_CALL_ID, "Task P")

    # Exactly once. The dead holder committed nothing and the thief committed
    # one mutation, so re-execution restored the work rather than duplicating it.
    assert _count_tasks(db) == 1
    assert _count_events(db, run_id) == 1

    replay = idempotency.acquire(run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, args_hash)
    assert replay.action is LeaseAction.REPLAY
    assert replay.result == result
    assert _count_tasks(db) == 1

    # Negative control, and the reason the guard has to be inside the UPDATE. A
    # live pending lease belongs to a holder that is still working, and stealing
    # it would re-run work that may be about to commit. An acquire that stole
    # unconditionally, or that read the expiry and then updated without the
    # guard, would pass every assertion above and fail here.
    live_run_id = _insert_run(db)
    live = idempotency.acquire(live_run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, args_hash)
    assert live.action is LeaseAction.EXECUTE

    held = _load_lease(db, live_run_id, LEASE_CALL_ID)
    assert held.lease_expires_at > datetime.now(timezone.utc)

    # Polls every 250ms, eight times, then gives up. It never returns a null
    # result for a pending row.
    with pytest.raises(PolicyError) as in_flight:
        idempotency.acquire(live_run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, args_hash)
    assert in_flight.value.code == "LEASE_IN_FLIGHT"
    assert in_flight.value.http_status == 409

    still_held = _load_lease(db, live_run_id, LEASE_CALL_ID)
    assert still_held.attempt == 1
    assert still_held.status is LeaseStatus.PENDING
    assert _count_tasks(db) == 1
