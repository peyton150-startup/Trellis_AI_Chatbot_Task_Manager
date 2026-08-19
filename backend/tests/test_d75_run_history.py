"""D-75 run history and ownership data movement.

These prove that narrowing a read did not narrow who may perform it. Each
statement on the model startup path now selects less than it used to, and the
property under test is that the refusal shape is unchanged: an owned run
yields its state, while a foreign run and a missing run remain one
indistinguishable refusal.

They live here rather than in `test_invariants.py` because D-29 fixes that file
at exactly the fifteen named invariants, and rather than in
`test_t17_continuity.py` because these are about the reads, not about D-67
eligibility.
"""

import json
import sys
from pathlib import Path
from uuid import UUID

import pytest
from psycopg.types.json import Json
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_core import to_jsonable_python

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import idempotency, policy, runs, sql
from app.db import pool
from app.errors import OutOfScopeError
from app.models import LeaseAction, ToolName


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")
MISSING_RUN_ID = UUID("00000000-0000-0000-0000-0000000000fe")
LEASE_CALL_ID = "call-d75-0001"


@pytest.fixture
def db():
    """Real PostgreSQL, state-free before and after each proof."""
    with pool.connection() as conn:
        conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
        conn.commit()
        try:
            yield conn
        finally:
            conn.rollback()
            conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
            conn.commit()


def _insert_run(conn, actor_id=ACTOR_ID):
    row = conn.execute(
        sql.INSERT_RUN,
        {
            "actor_id": actor_id,
            "prompt": "D-75 fixture",
            "model": "d75-fixture-model",
        },
    ).fetchone()
    conn.commit()
    return row["id"]


def test_load_history_narrow_read_preserves_ownership(db):
    """The narrowed history read keeps D-71's refusal shape exactly.

    `load_history` selects the history column alone rather than the whole run.
    The ownership predicate travels with the statement, so the three outcomes
    that mattered before the narrowing still hold: an owned run yields its
    canonical history, and a foreign run and a missing run remain the same
    refusal as each other.
    """
    owned = runs.create(ACTOR_ID, "owned prompt", "test-model")
    foreign = runs.create(OTHER_ACTOR_ID, "foreign prompt", "test-model")
    db.commit()

    assert runs.load_history(owned.id, ACTOR_ID) == []

    canonical = [
        {
            "parts": [{"content": "hello", "part_kind": "user-prompt"}],
            "kind": "request",
        }
    ]
    runs.save_history(owned.id, canonical)
    assert runs.load_history(owned.id, ACTOR_ID) == canonical

    with pytest.raises(OutOfScopeError):
        runs.load_history(foreign.id, ACTOR_ID)

    with pytest.raises(OutOfScopeError):
        runs.load_history(MISSING_RUN_ID, ACTOR_ID)


def test_load_history_narrow_read_selects_one_column(db):
    """The statement returns history alone, not a whole agent_runs row."""
    run = runs.create(ACTOR_ID, "shape probe", "test-model")
    db.commit()

    row = db.execute(
        sql.SELECT_RUN_HISTORY, {"run_id": run.id, "actor_id": ACTOR_ID}
    ).fetchone()
    db.commit()

    assert list(row.keys()) == ["message_history"]


def test_replay_preflight_is_actor_scoped(db):
    """`replay_completed` refuses a foreign run before it reveals anything.

    The preflight resolves ownership first and terminally. This proves the
    predicate is real rather than incidental: a completed lease sitting on
    another actor's run must not replay its stored result, and must not even
    disclose that the invocation exists. A missing run and a foreign run remain
    the same refusal, which is the property that keeps run ids from being an
    enumeration oracle.

    Before this test the whole suite passed with the actor predicate removed
    from the ownership statement, because nothing exercised this function
    directly. The narrowed read in `runs.assert_owned` is only safe if that
    predicate is proven, so it is proven here.
    """
    foreign_run_id = _insert_run(db, actor_id=OTHER_ACTOR_ID)
    arguments = {"title": "Foreign task"}
    args_hash = policy.arguments_hash(arguments)

    lease = idempotency.acquire(
        foreign_run_id, LEASE_CALL_ID, ToolName.CREATE_TASK, args_hash
    )
    assert lease.action is LeaseAction.EXECUTE
    idempotency.complete(
        foreign_run_id, LEASE_CALL_ID, {"task_id": "foreign-result"}, conn=db
    )
    db.commit()

    # The owner replays normally. This anchors the test: the row really is
    # completed and really is replayable, so the refusal below is about the
    # actor and nothing else.
    assert (
        idempotency.replay_completed(
            foreign_run_id,
            LEASE_CALL_ID,
            ToolName.CREATE_TASK,
            args_hash,
            actor_id=OTHER_ACTOR_ID,
        )
        == {"task_id": "foreign-result"}
    )

    # A different actor gets the out-of-scope refusal, not the stored result
    # and not a conflict that would confirm the call id exists.
    with pytest.raises(OutOfScopeError):
        idempotency.replay_completed(
            foreign_run_id,
            LEASE_CALL_ID,
            ToolName.CREATE_TASK,
            args_hash,
            actor_id=ACTOR_ID,
        )

    # Missing is indistinguishable from foreign.
    with pytest.raises(OutOfScopeError):
        idempotency.replay_completed(
            MISSING_RUN_ID,
            LEASE_CALL_ID,
            ToolName.CREATE_TASK,
            args_hash,
            actor_id=ACTOR_ID,
        )


def test_history_conversion_survives_a_real_jsonb_round_trip(db):
    """The save and load conversions are equivalent through real PostgreSQL.

    D-75 changed how history crosses the boundary in both directions:
    `to_jsonable_python` on the way out and `validate_python` on the way back,
    replacing a pair that each built a complete intermediate JSON encoding.
    Proving that by having the new serializer and the new loader agree with
    each other proves nothing, because a shared misrepresentation would agree
    with itself. The authority is a real jsonb column.

    So this stores both encodings, compares the rows PostgreSQL actually holds,
    reloads both, and compares the reconstructed messages. It uses the message
    parts this path really carries rather than a plain dict, `ThinkingPart`
    included: reasoning is the part most likely to be dropped or flattened by a
    serialization change, and it is the one whose survival is least obvious
    from reading either function.
    """
    messages = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="system"),
                UserPromptPart(content="add a task"),
            ]
        ),
        ModelResponse(
            parts=[
                ThinkingPart(content="deliberation", provider_name="openai"),
                ToolCallPart(
                    tool_name="create_task",
                    args={"title": "Repair the fence"},
                    tool_call_id="call-d75-conv",
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="create_task",
                    content={"task_id": "0f9c"},
                    tool_call_id="call-d75-conv",
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content="Created it.")]),
    ]

    previous = json.loads(ModelMessagesTypeAdapter.dump_json(messages))
    current = to_jsonable_python(messages)

    db.execute("CREATE TEMP TABLE d75_conversion (k text primary key, v jsonb)")
    db.execute(
        "INSERT INTO d75_conversion VALUES (%s, %s), (%s, %s)",
        ("previous", Json(previous), "current", Json(current)),
    )
    stored_previous = db.execute(
        "SELECT v FROM d75_conversion WHERE k = 'previous'"
    ).fetchone()["v"]
    stored_current = db.execute(
        "SELECT v FROM d75_conversion WHERE k = 'current'"
    ).fetchone()["v"]
    db.commit()

    # What PostgreSQL holds is identical, so the column cannot tell the two
    # encodings apart.
    assert stored_previous == stored_current

    reloaded_previous = ModelMessagesTypeAdapter.validate_python(stored_previous)
    reloaded_current = ModelMessagesTypeAdapter.validate_python(stored_current)

    # And what comes back is identical to each other and to what went in.
    assert ModelMessagesTypeAdapter.dump_json(
        reloaded_previous
    ) == ModelMessagesTypeAdapter.dump_json(reloaded_current)
    assert ModelMessagesTypeAdapter.dump_json(
        reloaded_current
    ) == ModelMessagesTypeAdapter.dump_json(messages)

    # The reasoning part specifically survived rather than being flattened
    # into the visible text of the response.
    thinking = [
        part
        for message in reloaded_current
        for part in message.parts
        if isinstance(part, ThinkingPart)
    ]
    assert len(thinking) == 1
    assert thinking[0].content == "deliberation"
