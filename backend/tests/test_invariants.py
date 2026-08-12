"""The thirteen deterministic invariant tests from BUILD_SPEC section 11.

T04 owns six of them. The remaining seven belong to T05, T07, and T08 and are
deliberately absent rather than skipped, so the count of collected tests always
matches the count of proven invariants.

None of these constructs an Agent, calls a model, or makes a network call. They
call the policy layer directly against real PostgreSQL, because policy.check
reads task ownership from the database and a faked lookup would prove only the
fake. Section 6 requires a missing row and another actor's row to produce the
identical error, which is a claim about what the scope query actually returns.

Fixture values are round and hand-checkable per section 11: no randomness, no
faker, and no dependence on the current time except the expired approval, which
is anchored by a negative TTL against the database clock rather than by
injecting a clock into the kernel. See D-15.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
from psycopg.types.json import Json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import policy, sql
from app.config import settings
from app.db import pool
from app.errors import PolicyError
from app.models import Approval, ToolName


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")
MISSING_TASK_ID = UUID("00000000-0000-0000-0000-0000000000ff")

TOOL_CALL_ID = "call-t04-0001"
OTHER_TOOL_CALL_ID = "call-t04-0002"

TODAY = date(2026, 8, 17)

# INSERT_APPROVAL computes expires_at as now() + make_interval(secs => ttl), so a
# negative TTL produces a row that is already expired against the database clock.
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
