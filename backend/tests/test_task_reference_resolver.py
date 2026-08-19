"""Focused deterministic tests for task-reference discovery."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from app import domain, sql, tools
from app.db import pool
from app.errors import (
    IdempotencyConflictError,
    OutOfScopeError,
    VersionConflictError,
)
from app.models import (
    CreateTaskArgs,
    DeleteTasksArgs,
    GetTaskHistoryArgs,
    ResolveTaskReferenceArgs,
    TaskHistoryEffect,
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


def _run(conn, actor_id):
    row = conn.execute(
        sql.INSERT_RUN,
        {
            "actor_id": actor_id,
            "prompt": "resolver fixture",
            "model": "resolver-test",
        },
    ).fetchone()
    conn.commit()
    return row["id"]


def _commit(conn, actor_id, mutation):
    domain.write_events(_run(conn, actor_id), actor_id, mutation.events, conn=conn)
    conn.commit()
    return mutation.tasks[0]


def _create(conn, actor_id, title):
    return _commit(
        conn,
        actor_id,
        domain.create_task(actor_id, CreateTaskArgs(title=title), conn=conn),
    )


def _rename(conn, actor_id, task, title):
    return _commit(
        conn,
        actor_id,
        domain.update_task(
            actor_id,
            UpdateTaskArgs(
                task_id=task.id,
                expected_version=task.version,
                title=title,
            ),
            conn=conn,
        ),
    )


def _touch_notes(conn, actor_id, task, notes):
    return _commit(
        conn,
        actor_id,
        domain.update_task(
            actor_id,
            UpdateTaskArgs(
                task_id=task.id,
                expected_version=task.version,
                notes=notes,
            ),
            conn=conn,
        ),
    )


def test_resolver_returns_unique_current_exact_match(db):
    task = _create(db, ACTOR_ID, "Repair north pasture fence")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair north pasture fence"),
        conn=db,
    )

    assert result.resolved is not None
    assert result.resolved.task_id == task.id
    assert len(result.candidates) == 1

    candidate = result.candidates[0]
    assert candidate.task_id == task.id
    assert candidate.matched_title == task.title
    assert candidate.current_title == task.title
    assert candidate.current_version == 1
    assert candidate.exists_now is True


def test_resolver_finds_historical_title_after_rename(db):
    task = _create(db, ACTOR_ID, "Repair creek fence")
    renamed = _rename(db, ACTOR_ID, task, "Repair north pasture fence")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair creek fence"),
        conn=db,
    )

    assert result.resolved is not None
    assert result.resolved.task_id == task.id
    assert len(result.candidates) == 1

    candidate = result.candidates[0]
    assert candidate.task_id == task.id
    assert candidate.matched_title == "Repair creek fence"
    assert candidate.current_title == renamed.title
    assert candidate.current_version == 2
    assert candidate.exists_now is True


def test_resolver_finds_deleted_task_from_history(db):
    task = _create(db, ACTOR_ID, "Retire old harvester")

    mutation = domain.delete_tasks(
        ACTOR_ID,
        DeleteTasksArgs(task_ids=[task.id]),
        conn=db,
    )
    domain.write_events(_run(db, ACTOR_ID), ACTOR_ID, mutation.events, conn=db)
    db.commit()

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Retire old harvester"),
        conn=db,
    )

    assert result.resolved is not None
    assert result.resolved.task_id == task.id
    assert len(result.candidates) == 1

    candidate = result.candidates[0]
    assert candidate.task_id == task.id
    assert candidate.current_title is None
    assert candidate.current_version is None
    assert candidate.exists_now is False


def test_resolver_does_not_guess_between_duplicate_titles(db):
    first = _create(db, ACTOR_ID, "Repair fence")
    second = _create(db, ACTOR_ID, "Repair fence")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence"),
        conn=db,
    )

    assert result.resolved is None
    assert {item.task_id for item in result.candidates} == {
        first.id,
        second.id,
    }


def test_resolver_excludes_foreign_actor_history(db):
    _create(db, OTHER_ACTOR_ID, "Private north fence")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Private north fence"),
        conn=db,
    )

    assert result.resolved is None
    assert result.candidates == []


def test_resolver_returns_unresolved_for_no_match(db):
    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Definitely does not exist"),
        conn=db,
    )

    assert result.resolved is None
    assert result.candidates == []


def test_resolver_tool_is_tracked_and_read_only(db):
    task = _create(db, ACTOR_ID, "Find the orchard gate")
    run_id = _run(db, ACTOR_ID)

    before_events = db.execute(
        "SELECT count(*) AS n FROM task_events"
    ).fetchone()["n"]

    ctx = tools.ToolContext(
        actor_id=ACTOR_ID,
        run_id=run_id,
        tool_call_id="resolver-read-only",
    )
    args = ResolveTaskReferenceArgs(reference="orchard gate")

    result = tools.resolve_task_reference(ctx, args)

    assert result["resolved"]["task_id"] == str(task.id)
    assert len(result["candidates"]) == 1

    after_events = db.execute(
        "SELECT count(*) AS n FROM task_events"
    ).fetchone()["n"]
    assert after_events == before_events

    approvals = db.execute(
        "SELECT count(*) AS n FROM approvals WHERE run_id = %s",
        (run_id,),
    ).fetchone()["n"]
    assert approvals == 0

    lease = db.execute(
        """
        SELECT tool_name, status, result
        FROM tool_invocations
        WHERE run_id = %s AND tool_call_id = %s
        """,
        (run_id, "resolver-read-only"),
    ).fetchone()

    assert lease["tool_name"] == "resolve_task_reference"
    assert lease["status"] == "completed"
    assert lease["result"] == result


def test_resolver_tool_replays_stored_result(db):
    task = _create(db, ACTOR_ID, "Old barn roof")
    run_id = _run(db, ACTOR_ID)

    ctx = tools.ToolContext(
        actor_id=ACTOR_ID,
        run_id=run_id,
        tool_call_id="resolver-replay",
    )
    args = ResolveTaskReferenceArgs(reference="Old barn roof")

    first = tools.resolve_task_reference(ctx, args)

    _rename(db, ACTOR_ID, task, "New barn roof")

    replayed = tools.resolve_task_reference(ctx, args)

    assert replayed == first
    assert replayed["resolved"]["task_id"] == str(task.id)
    assert replayed["candidates"][0]["current_title"] == "Old barn roof"


def test_resolver_respects_candidate_limit(db):
    _create(db, ACTOR_ID, "Fence alpha")
    _create(db, ACTOR_ID, "Fence beta")
    _create(db, ACTOR_ID, "Fence gamma")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Fence", limit=2),
        conn=db,
    )

    assert result.resolved is None
    assert len(result.candidates) == 2
    assert all("Fence" in item.matched_title for item in result.candidates)


def test_resolver_argument_limit_is_bounded():
    ResolveTaskReferenceArgs(reference="Fence", limit=2)
    ResolveTaskReferenceArgs(reference="Fence", limit=20)

    with pytest.raises(ValidationError):
        ResolveTaskReferenceArgs(reference="Fence", limit=1)

    with pytest.raises(ValidationError):
        ResolveTaskReferenceArgs(reference="Fence", limit=21)



# ------------------------------------------------ D-73 exact vs substring

# The rule these pin: one exact title wins over any number of weaker substring
# matches, while two exact task ids stay ambiguous. Exactness is decided by
# PostgreSQL's `lower(...)` comparison in SELECT_TASK_REFERENCE_CANDIDATES, and
# domain reads that decision rather than recomputing it, so there is one
# definition of "exact" in the system rather than two that can drift apart.


def test_resolver_prefers_a_unique_exact_title_over_a_substring(db):
    exact = _create(db, ACTOR_ID, "Repair fence")
    wider = _create(db, ACTOR_ID, "Repair fence north")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence"),
        conn=db,
    )

    assert result.resolved is not None
    assert result.resolved.task_id == exact.id
    assert {item.task_id for item in result.candidates} == {exact.id, wider.id}

    # The weaker match is still reported, so the model can offer it, but it did
    # not block the deterministic decision.
    assert result.candidates[0].task_id == exact.id


def test_resolver_prefers_a_unique_exact_title_over_many_substrings(db):
    exact = _create(db, ACTOR_ID, "Repair fence")
    _create(db, ACTOR_ID, "Repair fence north")
    _create(db, ACTOR_ID, "Repair fence south")
    _create(db, ACTOR_ID, "Repair fence gate hinge")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence"),
        conn=db,
    )

    assert result.resolved is not None
    assert result.resolved.task_id == exact.id
    assert len(result.candidates) == 4


def test_resolver_matches_an_exact_title_case_insensitively(db):
    task = _create(db, ACTOR_ID, "Repair North Pasture Fence")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="repair north pasture fence"),
        conn=db,
    )

    assert result.resolved is not None
    assert result.resolved.task_id == task.id
    assert result.resolved.current_version == 1
    assert result.resolved.exists_now is True


def test_resolver_resolves_a_single_substring_only_match(db):
    task = _create(db, ACTOR_ID, "Repair fence north")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="fence north"),
        conn=db,
    )

    assert result.resolved is not None
    assert result.resolved.task_id == task.id
    assert len(result.candidates) == 1


def test_resolver_does_not_guess_between_substring_only_matches(db):
    first = _create(db, ACTOR_ID, "Repair fence north")
    second = _create(db, ACTOR_ID, "Repair fence south")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence"),
        conn=db,
    )

    assert result.resolved is None
    assert {item.task_id for item in result.candidates} == {first.id, second.id}


def test_resolver_prefers_a_historical_exact_over_a_current_substring(db):
    renamed = _create(db, ACTOR_ID, "Repair fence")
    _rename(db, ACTOR_ID, renamed, "Repair the west boundary")
    wider = _create(db, ACTOR_ID, "Repair fence north")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence"),
        conn=db,
    )

    # The exact title exists only in history, and the substring match is a live
    # task. Exactness outranks recency, so the renamed task still wins.
    assert result.resolved is not None
    assert result.resolved.task_id == renamed.id
    assert result.resolved.current_title == "Repair the west boundary"
    assert result.resolved.exists_now is True
    assert {item.task_id for item in result.candidates} == {renamed.id, wider.id}


def test_resolver_reports_one_candidate_for_a_current_and_historical_exact(db):
    task = _create(db, ACTOR_ID, "Repair fence")
    interim = _rename(db, ACTOR_ID, task, "Repair gate")
    _rename(db, ACTOR_ID, interim, "Repair fence")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence"),
        conn=db,
    )

    # The title is exact on the live row and on two separate event snapshots.
    # Per-task dedupe must collapse those into one candidate rather than
    # manufacturing ambiguity against the task itself.
    assert result.resolved is not None
    assert result.resolved.task_id == task.id
    assert len(result.candidates) == 1
    assert result.candidates[0].current_version == 3


def test_resolver_reports_one_candidate_when_a_title_repeats_across_events(db):
    task = _create(db, ACTOR_ID, "Repair fence")
    second = _touch_notes(db, ACTOR_ID, task, "Check the hinges")
    _touch_notes(db, ACTOR_ID, second, "Bring the post driver")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence"),
        conn=db,
    )

    # Three events, each carrying the same title in before and/or after.
    assert result.resolved is not None
    assert result.resolved.task_id == task.id
    assert len(result.candidates) == 1


def test_resolver_resolves_an_exact_match_at_the_smallest_limit(db):
    exact = _create(db, ACTOR_ID, "Repair fence")
    _create(db, ACTOR_ID, "Repair fence north")
    _create(db, ACTOR_ID, "Repair fence south")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence", limit=2),
        conn=db,
    )

    # Exact rows sort ahead of substring rows before LIMIT applies, so the
    # smallest accepted window cannot hide the exact winner.
    assert result.resolved is not None
    assert result.resolved.task_id == exact.id
    assert len(result.candidates) == 2


def test_truncation_cannot_manufacture_a_resolved_task(db):
    _create(db, ACTOR_ID, "Fence alpha")
    _create(db, ACTOR_ID, "Fence beta")
    _create(db, ACTOR_ID, "Fence gamma")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Fence", limit=2),
        conn=db,
    )

    # Three substring matches truncated to two. The floor of two is what makes
    # this safe: a second competing candidate always survives truncation, so a
    # short window can never look unique.
    assert result.resolved is None
    assert len(result.candidates) == 2


def test_duplicate_exact_titles_stay_ambiguous_at_the_smallest_limit(db):
    first = _create(db, ACTOR_ID, "Repair fence")
    second = _create(db, ACTOR_ID, "Repair fence")
    _create(db, ACTOR_ID, "Repair fence north")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence", limit=2),
        conn=db,
    )

    # Both exact rows outrank the substring row, so both survive LIMIT 2 and the
    # ambiguity is still visible. This is the property that lets exact-wins be
    # safe on a bounded query.
    assert result.resolved is None
    assert {item.task_id for item in result.candidates} == {first.id, second.id}



def test_rank_not_alphabetical_luck_keeps_the_exact_match_inside_the_limit(db):
    exact = _create(db, ACTOR_ID, "Fence")
    _create(db, ACTOR_ID, "Alpha fence post")
    _create(db, ACTOR_ID, "Bravo fence rail")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Fence", limit=2),
        conn=db,
    )

    # Deliberately adversarial to the secondary sort. The exact title sorts LAST
    # alphabetically here, so if ORDER BY stopped leading with match_rank the two
    # substring rows would fill the window and the exact match would be truncated
    # away. The earlier smallest-limit test cannot catch that, because there the
    # exact title is a prefix of the others and sorts first either way.
    assert result.resolved is not None
    assert result.resolved.task_id == exact.id
    assert result.candidates[0].task_id == exact.id
    assert len(result.candidates) == 2


def test_rank_not_alphabetical_luck_keeps_duplicate_exacts_visible(db):
    first = _create(db, ACTOR_ID, "Fence")
    second = _create(db, ACTOR_ID, "Fence")
    _create(db, ACTOR_ID, "Alpha fence post")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Fence", limit=2),
        conn=db,
    )

    # The same adversarial ordering applied to ambiguity. Both exact rows must
    # survive LIMIT 2 ahead of the alphabetically earlier substring, or the
    # second exact would be hidden and the first would look unique.
    assert result.resolved is None
    assert {item.task_id for item in result.candidates} == {first.id, second.id}

def test_resolver_candidate_order_is_deterministic(db):
    _create(db, ACTOR_ID, "Fence gamma")
    _create(db, ACTOR_ID, "Fence alpha")
    _create(db, ACTOR_ID, "Fence beta")

    orders = []
    for _ in range(3):
        result = domain.resolve_task_reference(
            ACTOR_ID,
            ResolveTaskReferenceArgs(reference="Fence"),
            conn=db,
        )
        orders.append([item.task_id for item in result.candidates])

    # The SQL tie breakers end in task_id, so repeated reads of unchanged rows
    # must agree. Pinning this protects the ordering against future query edits.
    assert orders[0] == orders[1] == orders[2]
    assert len(orders[0]) == 3


# ------------------------------------------------- D-73 reference normalization

# `_payload()` hashes the already-validated arguments model, so normalization has
# to happen at the typed boundary or it happens after the identity is fixed.
# Stripping inside domain would leave " Fence " and "Fence" running the same
# search under two different argument hashes.


def test_resolver_arguments_normalize_surrounding_whitespace():
    assert ResolveTaskReferenceArgs(reference="  Repair fence  ").reference == (
        "Repair fence"
    )
    assert ResolveTaskReferenceArgs(reference="Repair fence\n").reference == (
        "Repair fence"
    )


def test_resolver_arguments_reject_a_blank_reference():
    for blank in ("", " ", "   ", "\t", "\n"):
        with pytest.raises(ValidationError):
            ResolveTaskReferenceArgs(reference=blank)


def test_resolver_arguments_preserve_case():
    # Search is case-insensitive, but the reference is not case-folded. Two
    # spellings stay distinct idempotency identities in D-73 v1.
    assert ResolveTaskReferenceArgs(reference="Fence").reference == "Fence"
    assert ResolveTaskReferenceArgs(reference="fence").reference == "fence"


def test_resolver_arguments_measure_length_after_normalization():
    assert len(ResolveTaskReferenceArgs(reference=" " + "f" * 500 + " ").reference) == (
        500
    )

    with pytest.raises(ValidationError):
        ResolveTaskReferenceArgs(reference="f" * 501)


def test_padded_reference_replays_the_stored_result(db):
    task = _create(db, ACTOR_ID, "Repair fence")
    run_id = _run(db, ACTOR_ID)

    ctx = tools.ToolContext(
        actor_id=ACTOR_ID,
        run_id=run_id,
        tool_call_id="resolver-normalized",
    )

    first = tools.resolve_task_reference(
        ctx, ResolveTaskReferenceArgs(reference="Repair fence")
    )
    replayed = tools.resolve_task_reference(
        ctx, ResolveTaskReferenceArgs(reference="  Repair fence  ")
    )

    # Same run, same tool call, padded spelling. Normalization happens before
    # the arguments hash, so this is the same call and replays rather than
    # raising an argument conflict.
    assert replayed == first
    assert replayed["resolved"]["task_id"] == str(task.id)

    invocations = db.execute(
        "SELECT count(*) AS n FROM tool_invocations WHERE run_id = %s",
        (run_id,),
    ).fetchone()["n"]
    assert invocations == 1


def test_a_changed_reference_conflicts_instead_of_replaying(db):
    _create(db, ACTOR_ID, "Repair fence")
    _create(db, ACTOR_ID, "Inspect barn roof")
    run_id = _run(db, ACTOR_ID)

    ctx = tools.ToolContext(
        actor_id=ACTOR_ID,
        run_id=run_id,
        tool_call_id="resolver-conflict",
    )

    tools.resolve_task_reference(
        ctx, ResolveTaskReferenceArgs(reference="Repair fence")
    )

    # Same run and same tool call id, genuinely different arguments. This must
    # not hand back the unrelated stored result.
    with pytest.raises(IdempotencyConflictError):
        tools.resolve_task_reference(
            ctx, ResolveTaskReferenceArgs(reference="Inspect barn roof")
        )


def test_the_wire_response_hides_the_ranking_and_authority_fields(db):
    task = _create(db, ACTOR_ID, "Repair fence")
    _create(db, ACTOR_ID, "Repair fence north")

    payload = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Repair fence"),
        conn=db,
    ).model_dump(mode="json")

    assert set(payload) == {"reference", "resolved", "candidates"}
    assert payload["resolved"]["task_id"] == str(task.id)

    for candidate in payload["candidates"] + [payload["resolved"]]:
        assert set(candidate) == {
            "task_id",
            "matched_title",
            "current_title",
            "current_version",
            "exists_now",
        }
        # match_rank is how domain decided, not something the caller re-decides.
        assert "match_rank" not in candidate
        # No owner, no storage timestamps, no raw snapshot.
        assert "owner_id" not in candidate
        assert "created_at" not in candidate
        assert "updated_at" not in candidate


# ------------------------------------------- D-73 closure with D-71 and D-72

# The property these prove is composition, not two features working alone:
# whatever the resolver says is the actor's task, history must agree is in the
# actor's authority set. `resolve_task_reference` and `read_task_history` draw on
# the same two predicates, actor-owned `tasks` rows and actor-written
# `task_events` rows, so this should hold structurally. These execute it.


def _resolve(conn, actor_id, reference):
    return domain.resolve_task_reference(
        actor_id,
        ResolveTaskReferenceArgs(reference=reference),
        conn=conn,
    )


def test_a_resolved_current_task_is_readable_history(db):
    task = _create(db, ACTOR_ID, "Repair north fence")

    resolved = _resolve(db, ACTOR_ID, "Repair north fence").resolved
    assert resolved is not None

    history = domain.read_task_history(
        ACTOR_ID, resolved.task_id, limit=20, before_event_id=None, conn=db
    )

    assert history.task_id == task.id
    assert history.exists_now is resolved.exists_now
    assert history.current_version == resolved.current_version


def test_a_task_resolved_by_its_old_title_is_readable_history(db):
    task = _create(db, ACTOR_ID, "Repair old fence")
    _rename(db, ACTOR_ID, task, "Repair north fence")

    resolved = _resolve(db, ACTOR_ID, "Repair old fence").resolved
    assert resolved is not None
    assert resolved.task_id == task.id

    history = domain.read_task_history(
        ACTOR_ID, resolved.task_id, limit=20, before_event_id=None, conn=db
    )

    # The rename is visible from the discarded name, which is the point of
    # searching historical titles at all.
    assert history.exists_now is True
    assert [entry.effect for entry in history.entries] == [
        TaskHistoryEffect.UPDATED,
        TaskHistoryEffect.CREATED,
    ]


def test_a_deleted_task_resolves_to_its_deletion_boundary_snapshot(db):
    task = _create(db, ACTOR_ID, "Inspect barn roof")
    updated = _touch_notes(db, ACTOR_ID, task, "Bring the tall ladder")

    mutation = domain.delete_tasks(
        ACTOR_ID, DeleteTasksArgs(task_ids=[updated.id]), conn=db
    )
    domain.write_events(_run(db, ACTOR_ID), ACTOR_ID, mutation.events, conn=db)
    db.commit()

    resolved = _resolve(db, ACTOR_ID, "Inspect barn roof").resolved
    assert resolved is not None
    assert resolved.exists_now is False
    assert resolved.current_version is None

    history = domain.read_task_history(
        ACTOR_ID, resolved.task_id, limit=20, before_event_id=None, conn=db
    )

    assert history.exists_now is False

    deletion = history.entries[0]
    assert deletion.effect is TaskHistoryEffect.DELETED
    assert deletion.changes == []

    # D-72: the boundary snapshot comes from the stored `before`, not from
    # reconstructing state that no longer exists.
    assert deletion.snapshot is not None
    assert deletion.snapshot.title == "Inspect barn roof"
    assert deletion.snapshot.notes == "Bring the tall ladder"

    creation = history.entries[-1]
    assert creation.effect is TaskHistoryEffect.CREATED
    assert creation.changes == []
    assert creation.snapshot is not None
    assert creation.snapshot.notes == ""


def test_a_foreign_title_is_invisible_and_its_history_is_out_of_scope(db):
    foreign = _create(db, OTHER_ACTOR_ID, "Repair north fence")
    _rename(db, OTHER_ACTOR_ID, foreign, "Repair the west boundary")

    for reference in (
        "Repair north fence",
        "Repair the west boundary",
        "fence",
    ):
        result = _resolve(db, ACTOR_ID, reference)
        assert result.resolved is None
        assert result.candidates == []

    # Discovery and history agree that the task does not exist for this actor,
    # and neither reveals the id, version, or either title.
    with pytest.raises(OutOfScopeError):
        domain.read_task_history(
            ACTOR_ID, foreign.id, limit=20, before_event_id=None, conn=db
        )


def test_a_shared_title_resolves_to_each_actor_own_task(db):
    mine = _create(db, ACTOR_ID, "Repair north fence")
    theirs = _create(db, OTHER_ACTOR_ID, "Repair north fence")

    ours = _resolve(db, ACTOR_ID, "Repair north fence")
    yours = _resolve(db, OTHER_ACTOR_ID, "Repair north fence")

    assert ours.resolved.task_id == mine.id
    assert yours.resolved.task_id == theirs.id

    # A colliding title must not become ambiguity. Each actor sees exactly one
    # candidate, so neither learns the other task exists.
    assert len(ours.candidates) == 1
    assert len(yours.candidates) == 1


def test_resolver_then_history_are_two_tracked_reads_in_one_run(db):
    task = _create(db, ACTOR_ID, "Repair north fence")
    run_id = _run(db, ACTOR_ID)

    before_events = db.execute(
        "SELECT count(*) AS n FROM task_events"
    ).fetchone()["n"]

    resolved = tools.resolve_task_reference(
        tools.ToolContext(
            actor_id=ACTOR_ID, run_id=run_id, tool_call_id="compose-resolve"
        ),
        ResolveTaskReferenceArgs(reference="Repair north fence"),
    )["resolved"]

    history = tools.get_task_history(
        tools.ToolContext(
            actor_id=ACTOR_ID, run_id=run_id, tool_call_id="compose-history"
        ),
        GetTaskHistoryArgs(task_id=UUID(resolved["task_id"])),
    )

    assert history["task_id"] == str(task.id)
    assert history["current_version"] == resolved["current_version"]

    tracked = db.execute(
        """
        SELECT tool_name, status
          FROM tool_invocations
         WHERE run_id = %s
         ORDER BY tool_name
        """,
        (run_id,),
    ).fetchall()
    assert [(row["tool_name"], row["status"]) for row in tracked] == [
        ("get_task_history", "completed"),
        ("resolve_task_reference", "completed"),
    ]

    after_events = db.execute(
        "SELECT count(*) AS n FROM task_events"
    ).fetchone()["n"]
    assert after_events == before_events

    approvals = db.execute(
        "SELECT count(*) AS n FROM approvals WHERE run_id = %s", (run_id,)
    ).fetchone()["n"]
    assert approvals == 0


def test_a_stale_resolver_replay_cannot_bypass_optimistic_concurrency(db):
    task = _create(db, ACTOR_ID, "Repair north fence")
    run_id = _run(db, ACTOR_ID)

    ctx = tools.ToolContext(
        actor_id=ACTOR_ID, run_id=run_id, tool_call_id="stale-resolver"
    )
    args = ResolveTaskReferenceArgs(reference="Repair north fence")

    first = tools.resolve_task_reference(ctx, args)
    assert first["resolved"]["current_version"] == 1

    _touch_notes(db, ACTOR_ID, task, "Someone else got here first")

    replayed = tools.resolve_task_reference(ctx, args)

    # An idempotent read replays what it stored. It must not silently re-read
    # newer state, so the replay is legitimately stale.
    assert replayed == first
    assert replayed["resolved"]["current_version"] == 1

    with pytest.raises(VersionConflictError):
        tools.update_task(
            tools.ToolContext(
                actor_id=ACTOR_ID, run_id=run_id, tool_call_id="stale-update"
            ),
            UpdateTaskArgs(
                task_id=task.id,
                expected_version=replayed["resolved"]["current_version"],
                notes="Written from a stale resolver result",
            ),
        )

    # Stale discovery cannot become a stale mutation. The kernel, not the
    # resolver, is what refuses it, and the newer state survives untouched.
    current = db.execute(
        "SELECT version, notes FROM tasks WHERE id = %s", (task.id,)
    ).fetchone()
    assert current["version"] == 2
    assert current["notes"] == "Someone else got here first"


def test_per_task_dedupe_keeps_the_strongest_match_for_that_task(db):
    task = _create(db, ACTOR_ID, "Fence")
    _rename(db, ACTOR_ID, task, "Fence north")
    other = _create(db, ACTOR_ID, "Fence south")

    result = domain.resolve_task_reference(
        ACTOR_ID,
        ResolveTaskReferenceArgs(reference="Fence"),
        conn=db,
    )

    # This task matches twice: its current title "Fence north" is a substring
    # hit, and its historical title "Fence" is exact. Dedupe keeps one row per
    # task and must keep the stronger one, or the task would be demoted to a
    # substring match and the query would report two substring candidates with
    # nothing exact, turning a decidable reference into a false ambiguity.
    assert result.resolved is not None
    assert result.resolved.task_id == task.id
    assert result.resolved.matched_title == "Fence"
    assert result.resolved.current_title == "Fence north"
    assert {item.task_id for item in result.candidates} == {task.id, other.id}
