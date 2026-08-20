"""D-76. Run-relative natural-language undo, as deterministic control flow.

What these tests are protecting is a distinction the browser transport makes and
the old single-locator design could not:

    trellisPreviousRunId    the newest server-issued application run, whatever
                            became of it, and the only undo target
    trellisContinuityRunId  the newest COMPLETED run, and only a source of
                            canonical conversation history

They are the same run on the happy path, which is exactly why targeting the
wrong one is easy to ship and hard to notice. They diverge when a run commits a
mutation and then fails, and that is the case a user is most likely to want
undone. `test_failed_previous_run_is_the_target_not_continuity` is the regression
that would catch a future change collapsing them back into one value.

The second property under test is that the model has no part in any of this. Not
"the model was mocked and returned nothing", but that constructing a model at all
fails the test: `_no_model_allowed` replaces both provider entry points with
functions that raise, so any control-path change that reaches `get_agent` shows
up as a failure here rather than as an NVIDIA bill.
"""

import json
import sys
import threading
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic_core import to_jsonable_python
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.function import FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent, domain, runs, sql, undo
from app.db import pool
from app.main import app
from app.models import (
    CreateTaskArgs,
    DeleteTasksArgs,
    RunStatus,
    UpdateTaskArgs,
)


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")
MISSING_RUN_ID = UUID("00000000-0000-0000-0000-0000000000ff")

NO_TARGET_TEXT = agent._NO_TARGET_TEXT


@pytest.fixture
def db():
    """Real PostgreSQL, state-free before and after each D-76 proof."""
    with pool.connection() as conn:
        conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
        conn.commit()
        try:
            yield conn
        finally:
            conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
            conn.commit()


@pytest.fixture(autouse=True)
def _no_model_allowed(monkeypatch):
    """Make provider construction itself a test failure.

    Asserting that a mock model returned nothing proves only that the mock was
    boring. This proves the control path never reaches the point where a model
    would exist, which is the actual D-76 claim. Tests that legitimately need a
    model undo this locally.
    """

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "the D-76 control path constructed a model; it must make zero "
            "provider requests and zero framework runs"
        )

    monkeypatch.setattr(agent, "_runtime_model", _forbidden)
    monkeypatch.setattr(agent, "get_agent", _forbidden)


# --------------------------------------------------------------- fixtures


def _run(actor_id=ACTOR_ID, *, status=RunStatus.COMPLETED, prompt="prior turn"):
    run = runs.create(actor_id, prompt, "d76-fixture-model")
    if status is RunStatus.RUNNING:
        return run
    return runs.set_status(run.id, status)


def _create_task(conn, run_id, title, actor_id=ACTOR_ID, **fields):
    mutation = domain.create_task(
        actor_id, CreateTaskArgs(title=title, **fields), conn=conn
    )
    domain.write_events(run_id, actor_id, mutation.events, conn=conn)
    conn.commit()
    return mutation.tasks[0]


def _delete_tasks(conn, run_id, task_ids, actor_id=ACTOR_ID):
    mutation = domain.delete_tasks(
        actor_id, DeleteTasksArgs(task_ids=list(task_ids)), conn=conn
    )
    domain.write_events(run_id, actor_id, mutation.events, conn=conn)
    conn.commit()
    return mutation.tasks


def _deleting_run(conn, titles, *, status=RunStatus.COMPLETED, actor_id=ACTOR_ID):
    """One run that deleted N tasks another run had created. The usual target."""
    setup = _run(actor_id)
    tasks = [_create_task(conn, setup.id, title, actor_id) for title in titles]

    run = runs.create(actor_id, "delete them", "d76-fixture-model")
    _delete_tasks(conn, run.id, [task.id for task in tasks], actor_id)
    if status is not RunStatus.RUNNING:
        runs.set_status(run.id, status)
    return run.id, tasks


def _tasks_by_id(conn):
    rows = conn.execute("SELECT * FROM tasks").fetchall()
    return {row["id"]: dict(row) for row in rows}


def _events_for(conn, run_id):
    return conn.execute(
        sql.SELECT_ALL_EVENTS_FOR_RUN, {"run_id": run_id}
    ).fetchall()


def _agui_body(message, *, previous_run_id=None, continuity_run_id=None):
    forwarded = {}
    if previous_run_id is not None:
        forwarded["trellisPreviousRunId"] = str(previous_run_id)
    if continuity_run_id is not None:
        forwarded["trellisContinuityRunId"] = str(continuity_run_id)

    return {
        "threadId": "client-thread-that-is-not-authority",
        "runId": "client-run-that-is-not-authority",
        "state": None,
        "messages": [
            {"id": "accepted-user-message", "role": "user", "content": message}
        ],
        "tools": [],
        "context": [],
        "forwardedProps": forwarded,
    }


def _events(response):
    """The AG-UI events of one control response, decoded in order."""
    decoded = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            decoded.append(json.loads(line[len("data: ") :]))
    return decoded


def _post(message, **locators):
    with TestClient(app) as client:
        response = client.post("/api/agui", json=_agui_body(message, **locators))
    return response


def _control_turn(message, **locators):
    """Post one control command and return (control run id, assistant text)."""
    response = _post(message, **locators)
    assert response.status_code == 200, response.text

    events = _events(response)
    kinds = [event["type"] for event in events]
    assert kinds == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ], kinds

    control_id = UUID(events[0]["threadId"])
    assert events[-1]["threadId"] == str(control_id)
    return control_id, events[2]["delta"]


# ------------------------------------------------------------- the grammar


def test_accepted_commands_are_intercepted_and_nothing_else_is():
    """Under-match on purpose. A miss costs a rephrase; a false hit undoes a run.

    Both halves matter equally. The negative list is every phrasing that names
    *what* to restore, and target interpretation is precisely what D-76 does not
    authorize the classifier to do.
    """
    for command in [
        "undo",
        "undo that",
        "Undo That",
        "  undo   that  ",
        "undo that.",
        "undo it",
        "undo what you just did?",
        "undo the last action",
        "revert that",
        "reverse that",
        "recover what you just deleted",
        "recover everything you just deleted",
        "restore what you just deleted",
        "restore everything you just deleted",
        "bring back what you just deleted",
        "bring back everything you just deleted",
        "bring back the tasks you just deleted!",
    ]:
        assert agent._is_undo_previous_command(command) is True, command

    for passthrough in [
        "restore D75",
        "restore Tractor",
        "restore my farm tasks",
        "undo yesterday's changes",
        "undo the fence change",
        "bring back all my old tasks",
        "recover task 123",
        "restore everything deleted this week",
        "undo that and then delete the fence task",
        "can you undo that",
        "please undo",
        "undothat",
        "",
    ]:
        assert agent._is_undo_previous_command(passthrough) is False, passthrough


def test_a_named_restore_reaches_the_model_path(db, monkeypatch):
    """"restore D75" is not a control command, so it goes where it always went.

    The model here is deterministic and does nothing. What is being proved is
    that the request took the D-67 branch at all, which shows up as a real
    `agent_runs` row carrying the configured model id rather than the control
    sentinel.
    """
    monkeypatch.setattr(agent, "get_agent", _text_only_agent)

    control_id, _ = _model_turn("restore D75")

    with pool.connection() as conn:
        row = conn.execute(
            "SELECT model FROM agent_runs WHERE id = %(id)s", {"id": control_id}
        ).fetchone()
    assert row["model"] != runs.CONTROL_TURN_MODEL


def _text_only_agent():
    async def _reply(messages, _info):
        yield "acknowledged"

    return agent.build_agent(FunctionModel(stream_function=_reply))


def _model_turn(message, **locators):
    response = _post(message, **locators)
    assert response.status_code == 200, response.text
    events = _events(response)
    return UUID(events[0]["threadId"]), events


# ------------------------------------------------------------- targeting


def test_previous_run_is_undone_under_original_identity(db):
    """The happy path, and the identity claim inside it.

    Restoration means the same row comes back, not that an equivalent one is
    created. Original id, all six restorable business fields, and `created_at`
    all survive; `version` and `updated_at` move forward, because compensation
    is a new forward mutation and history is never rewound.
    """
    run_id, tasks = _deleting_run(db, ["Run the farm"])
    original = tasks[0]

    control_id, text = _control_turn("undo that", previous_run_id=run_id)

    assert "Undone." in text
    assert control_id != run_id

    restored = _tasks_by_id(db)
    assert set(restored) == {original.id}

    row = restored[original.id]
    assert row["title"] == original.title
    assert row["notes"] == original.notes
    assert row["due_date"] == original.due_date
    assert row["priority"] == original.priority.value
    assert row["status"] == original.status.value
    assert row["blocked_by"] == original.blocked_by
    assert row["created_at"] == original.created_at
    assert row["version"] == original.version + 1
    assert row["updated_at"] > original.updated_at


def test_failed_previous_run_is_the_target_not_continuity(db):
    """The whole reason the two locators are separate values.

    A committed mutation whose response later failed leaves continuity pointing
    at the older completed run, by design. Undo must still target the failed one,
    because that is the action the user just watched happen.
    """
    completed_id, [survivor] = _deleting_run(db, ["Completed run deleted this"])
    undo.undo_run(completed_id, ACTOR_ID)
    assert survivor.id in _tasks_by_id(db)

    failed_id, [target] = _deleting_run(
        db, ["Failed run deleted this"], status=RunStatus.FAILED
    )

    _control_turn(
        "undo that",
        previous_run_id=failed_id,
        continuity_run_id=completed_id,
    )

    # The failed run's task came back. The completed run was not touched a
    # second time, which its unchanged compensation count proves.
    assert target.id in _tasks_by_id(db)
    completed_events = _events_for(db, completed_id)
    assert sum(1 for e in completed_events if e["operation"] == "restored") == 1


def test_interrupted_previous_run_is_undoable(db):
    """`interrupted` is eligible for the same reason `failed` is."""
    older_id, _ = _deleting_run(db, ["Older completed"])
    run_id, [target] = _deleting_run(
        db, ["Interrupted deleted this"], status=RunStatus.INTERRUPTED
    )

    _, text = _control_turn(
        "undo that", previous_run_id=run_id, continuity_run_id=older_id
    )

    assert "Undone." in text
    assert target.id in _tasks_by_id(db)


def test_an_invalid_target_never_falls_back_to_continuity(db):
    """No backward search, and no substitution of one locator for the other.

    Continuity here names a perfectly undoable run. The command still refuses,
    because the run the user named is the run the user named.
    """
    undoable_id, [task] = _deleting_run(db, ["Continuity deleted this"])

    _, text = _control_turn(
        "undo that",
        previous_run_id=MISSING_RUN_ID,
        continuity_run_id=undoable_id,
    )

    assert text == NO_TARGET_TEXT
    assert _tasks_by_id(db) == {}
    assert not any(e["operation"] == "restored" for e in _events_for(db, undoable_id))


def test_absent_foreign_and_missing_targets_are_one_refusal(db):
    """Indistinguishability, on the locator this decision introduces.

    A user who forges another actor's run id must learn exactly as much as a
    user who sent no id at all.
    """
    foreign_id, _ = _deleting_run(db, ["Not yours"], actor_id=OTHER_ACTOR_ID)

    _, absent = _control_turn("undo that")
    _, missing = _control_turn("undo that", previous_run_id=MISSING_RUN_ID)
    _, foreign = _control_turn("undo that", previous_run_id=foreign_id)

    assert absent == missing == foreign == NO_TARGET_TEXT

    # And the foreign run is untouched.
    assert not any(e["operation"] == "restored" for e in _events_for(db, foreign_id))


def test_malformed_previous_run_id_is_refused_before_anything_runs(db):
    """Same shape as the D-67 continuity locator: not a string, or not a UUID."""
    for value in ["not-a-uuid", 12, {"run": "id"}, ["id"]]:
        body = _agui_body("undo that")
        body["forwardedProps"]["trellisPreviousRunId"] = value
        with TestClient(app) as client:
            response = client.post("/api/agui", json=body)
        assert response.status_code == 403, (value, response.text)
        assert response.json()["error"]["code"] == "OUT_OF_SCOPE"

    # Nothing was created for a request that never got past extraction.
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM agent_runs").fetchone()["n"] == 0


@pytest.mark.parametrize(
    "status", [RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL]
)
def test_a_live_previous_run_is_refused(db, status):
    """D-44. Either status can still commit another tool call."""
    run_id, [task] = _deleting_run(db, ["Still moving"], status=status)

    _, text = _control_turn("undo that", previous_run_id=run_id)

    assert "has not finished yet" in text
    assert _tasks_by_id(db) == {}


def test_a_previous_run_with_no_effects_is_refused(db):
    """A conversational turn that changed nothing has nothing to compensate."""
    run_id = _run().id

    _, text = _control_turn("undo that", previous_run_id=run_id)

    assert "did not change any tasks" in text


def test_a_second_undo_refuses_and_never_redoes(db):
    """Undo applies once. The second attempt must not re-delete what it restored."""
    run_id, [task] = _deleting_run(db, ["Restore me once"])

    _, first = _control_turn("undo that", previous_run_id=run_id)
    assert "Undone." in first
    assert task.id in _tasks_by_id(db)

    _, second = _control_turn("undo that", previous_run_id=run_id)
    assert "already been undone" in second
    assert task.id in _tasks_by_id(db), "a second undo behaved like a redo"


def test_the_control_run_itself_is_not_undoable(db):
    """Which is what makes "undo that, undo that" safe with no special case.

    The browser advances `previousRunId` on every RUN_STARTED, so after a
    successful undo the next command targets the control run. Control runs
    commit no task events, so they refuse by the ordinary rule.
    """
    run_id, [task] = _deleting_run(db, ["Target"])

    control_id, _ = _control_turn("undo that", previous_run_id=run_id)
    _, text = _control_turn("undo that", previous_run_id=control_id)

    assert "did not change any tasks" in text
    assert task.id in _tasks_by_id(db)


# --------------------------------------------------------- kernel behaviour


def test_five_deleted_tasks_are_restored_all_or_nothing(db):
    """One run, one wave, no partial compensation."""
    titles = [f"Field {n}" for n in range(5)]
    run_id, tasks = _deleting_run(db, titles)
    assert _tasks_by_id(db) == {}

    _, text = _control_turn("recover everything you just deleted", previous_run_id=run_id)

    assert "Undone." in text
    assert "5 task changes" in text
    restored = _tasks_by_id(db)
    assert set(restored) == {task.id for task in tasks}
    for task in tasks:
        assert restored[task.id]["title"] == task.title
        assert restored[task.id]["created_at"] == task.created_at


def test_one_externally_modified_task_refuses_the_whole_wave(db):
    """All or nothing means zero restored, not four out of five."""
    titles = [f"Paddock {n}" for n in range(5)]
    setup = _run()
    tasks = [_create_task(db, setup.id, title) for title in titles]

    run_id = runs.create(ACTOR_ID, "delete them", "d76-fixture-model").id
    _delete_tasks(db, run_id, [task.id for task in tasks])
    runs.set_status(run_id, RunStatus.COMPLETED)

    # Someone recreates one of the deleted ids outside the run.
    db.execute(
        sql.INSERT_TASK_RESTORED,
        {
            **{
                name: getattr(tasks[2], name)
                for name in (
                    "id",
                    "title",
                    "notes",
                    "due_date",
                    "blocked_by",
                    "created_at",
                )
            },
            "owner_id": ACTOR_ID,
            "priority": tasks[2].priority.value,
            "status": tasks[2].status.value,
            "version": 99,
        },
    )
    db.commit()

    _, text = _control_turn("undo that", previous_run_id=run_id)

    assert "I did not undo anything" in text
    restored = _tasks_by_id(db)
    assert set(restored) == {tasks[2].id}, "a refused undo restored something"
    assert restored[tasks[2].id]["version"] == 99


def test_a_stale_pre_delete_version_stays_invalid_after_restore(db):
    """The ABA regression. Restoring the state must not restore the token.

    `version` is the optimistic-concurrency token: `UPDATE_TASK_GUARDED` accepts
    a write only when `expected_version` matches. If undo rewound the number, a
    client still holding the pre-deletion version would find its stale token
    valid again and would overwrite the restore without ever seeing it. That is
    why compensation moves the version forward instead.
    """
    run_id, [task] = _deleting_run(db, ["Tractor"])
    stale_expected_version = task.version

    _control_turn("undo that", previous_run_id=run_id)

    row = _tasks_by_id(db)[task.id]
    assert row["version"] > stale_expected_version

    conflicted = db.execute(
        sql.UPDATE_TASK_GUARDED,
        {
            "id": task.id,
            "owner_id": ACTOR_ID,
            "expected_version": stale_expected_version,
            "title": "written with a stale token",
            "notes": None,
            "due_date": None,
            "set_due_date": False,
            "priority": None,
            "status": None,
            "blocked_by": None,
            "set_blocked_by": False,
        },
    ).fetchone()
    db.commit()

    assert conflicted is None, "a stale pre-deletion version became valid again"
    assert _tasks_by_id(db)[task.id]["title"] == "Tractor"


def test_history_is_append_only_after_a_control_undo(db):
    """The original deletion event survives; the compensation is appended."""
    run_id, [task] = _deleting_run(db, ["Fence"])

    _control_turn("undo that", previous_run_id=run_id)

    operations = [row["operation"] for row in _events_for(db, run_id)]
    assert "deleted" in operations
    assert "restored" in operations
    assert operations.count("restored") == 1


# ------------------------------------------------------------- concurrency


def test_concurrent_undo_attempts_apply_at_most_once(db):
    """The TOCTOU regression, and what the row lock is actually load bearing for.

    Two assertions here, and the second is the one that pins the lock.

    At most one attempt applies. That much survives even without the lock, and
    an author mutation audit proved it: dropping `FOR UPDATE` left this
    invariant intact, because the kernel's own guards catch the losers. A
    delete-undo collides on the primary key, an update-undo collides on the
    version guard, and a create-undo finds its rows already gone. The safety
    property is genuinely defended twice.

    What the lock decides is what the loser is *told*. Under it, the second
    attempt blocks, wakes after the first commits, reads the compensation wave,
    and refuses as ALREADY_COMPENSATED, which is true and is the sentence D-76
    shows the user. Without it, the loser reaches the kernel and refuses with
    ROW_RECREATED, and the control turn then tells a human that someone else
    recreated their task. That is a false explanation of a correct outcome, and
    it is why the losers' reason is asserted here rather than only their count.
    """
    run_id, tasks = _deleting_run(db, ["Barn", "Silo", "Gate"])

    # Three, not more: `db.py` opens a default-sized pool, and a test that
    # asks for more connections than the pool holds would fail on capacity
    # rather than on the property under test. Three is enough to distinguish
    # "one winner" from "every caller wins".
    attempts = 3
    barrier = threading.Barrier(attempts)
    outcomes = []
    lock = threading.Lock()

    def attempt():
        barrier.wait(timeout=30)
        result = runs.attempt_run_undo(run_id, ACTOR_ID)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a concurrent undo attempt never returned"

    applied = [outcome for outcome in outcomes if outcome.applied > 0]
    assert len(applied) == 1, [
        (o.ineligible, None if o.result is None else o.result.reason) for o in outcomes
    ]
    assert applied[0].applied == 3

    # Every loser observed the winner's compensation wave rather than colliding
    # with it, which is only true while the eligibility check and the kernel run
    # inside one lock.
    losers = [outcome for outcome in outcomes if outcome.applied == 0]
    assert len(losers) == attempts - 1
    for loser in losers:
        assert loser.ineligible is runs.UndoIneligibility.ALREADY_COMPENSATED, (
            loser.ineligible,
            None if loser.result is None else loser.result.reason,
        )

    # Exactly one compensation wave exists, and every task came back once.
    events = _events_for(db, run_id)
    assert sum(1 for e in events if e["operation"] == "restored") == 3
    assert set(_tasks_by_id(db)) == {task.id for task in tasks}


# ------------------------------------------------------- run and history


def test_the_control_run_is_completed_before_run_finished(db):
    """Ordering the browser depends on, not cosmetics.

    `RunCompletionListener` fetches the run the moment it sees run end, and
    advances continuity only on `completed`. Emitting RUN_FINISHED first would
    lose the turn from the conversation for no reason but a self-inflicted race.
    """
    run_id, _ = _deleting_run(db, ["Ordering"])

    response = _post("undo that", previous_run_id=run_id)
    control_id = UUID(_events(response)[0]["threadId"])

    # The response body is fully consumed above, so RUN_FINISHED has been
    # emitted by the time this read happens.
    with TestClient(app) as client:
        detail = client.get(f"/api/runs/{control_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "completed"


def test_the_control_run_records_no_provider_and_no_usage(db):
    """The audit row must not claim NVIDIA acted."""
    run_id, _ = _deleting_run(db, ["Audit"])

    control_id, _ = _control_turn("undo that", previous_run_id=run_id)

    row = db.execute(
        "SELECT model, model_calls, tool_calls, input_tokens, output_tokens "
        "FROM agent_runs WHERE id = %(id)s",
        {"id": control_id},
    ).fetchone()
    assert row["model"] == runs.CONTROL_TURN_MODEL
    assert row["model_calls"] == 0
    assert row["tool_calls"] == 0
    assert row["input_tokens"] == 0
    assert row["output_tokens"] == 0

    # And it committed no task events of its own, which is what keeps it
    # permanently ineligible as an undo target.
    assert _events_for(db, control_id) == []


def test_synthetic_history_survives_the_real_persistence_boundary(db):
    """to_jsonable_python -> PostgreSQL jsonb -> ModelMessagesTypeAdapter.

    No `Agent` ran, so `_completion_recorder` has no result to record and these
    two messages are constructed directly. This is the proof that the pinned
    2.27.0 dataclasses survive the exact boundary this build already uses, and
    that nothing in them claims a provider request happened.
    """
    run_id, _ = _deleting_run(db, ["History"])

    control_id, text = _control_turn(
        "recover everything you just deleted", previous_run_id=run_id
    )

    stored = runs.load_history(control_id, ACTOR_ID)
    messages = ModelMessagesTypeAdapter.validate_python(stored)

    assert [message.kind for message in messages[-2:]] == ["request", "response"]
    request, response = messages[-2], messages[-1]

    assert request.parts[0].content == "recover everything you just deleted"
    assert response.parts[0].content == text
    assert request.metadata == {"trellis_control": "undo_previous"}
    assert response.metadata == {"trellis_control": "undo_previous"}

    # Nothing fabricated. No provider, no framework run, no reasoning.
    assert response.model_name is None
    assert response.provider_name is None
    assert response.run_id is None
    assert [part.part_kind for part in response.parts] == ["text"]
    for message in messages:
        assert not any(
            part.part_kind in {"thinking", "tool-call", "tool-return"}
            for part in message.parts
        )


def test_the_next_model_turn_inherits_the_control_turn(db, monkeypatch):
    """A control turn is an ordinary predecessor for the next real turn.

    The model does not need to be told an undo happened; it reads it from the
    canonical history the server owns.
    """
    monkeypatch.setattr(agent, "_runtime_model", agent._runtime_model)

    run_id, _ = _deleting_run(db, ["Inherited"])
    control_id, control_text = _control_turn(
        "recover everything you just deleted", previous_run_id=run_id
    )

    seen = []

    async def _recording(messages, _info):
        seen.append(list(messages))
        yield "reported"

    monkeypatch.setattr(
        agent, "get_agent", lambda: agent.build_agent(FunctionModel(stream_function=_recording))
    )

    _model_turn("What did you just restore?", continuity_run_id=control_id)

    assert seen, "the follow-up turn never reached the model"
    rendered = [
        part.content
        for message in seen[0]
        for part in message.parts
        if getattr(part, "content", None)
    ]
    assert "recover everything you just deleted" in rendered
    assert control_text in rendered


def test_the_history_predecessor_is_continuity_when_the_target_failed(db):
    """D-67 requires a completed predecessor; the undo target need not be one.

    So a failed target is undone while its canonical history comes from the
    completed continuity run, and neither authority borrows from the other.
    """
    completed_id, _ = _deleting_run(db, ["Older"])
    runs.save_history(
        completed_id,
        [
            {
                "parts": [
                    {
                        "content": "earlier conversation",
                        "timestamp": "2026-08-20T00:00:00Z",
                        "part_kind": "user-prompt",
                    }
                ],
                "kind": "request",
            }
        ],
    )

    failed_id, [task] = _deleting_run(db, ["Newer"], status=RunStatus.FAILED)

    control_id, _ = _control_turn(
        "undo that", previous_run_id=failed_id, continuity_run_id=completed_id
    )

    assert task.id in _tasks_by_id(db)

    history = runs.load_history(control_id, ACTOR_ID)
    contents = [
        part.get("content")
        for message in history
        for part in message.get("parts", [])
    ]
    assert "earlier conversation" in contents


def test_undo_works_with_no_history_predecessor_at_all(db):
    """Lack of conversation history never removes valid mutation authority."""
    run_id, [task] = _deleting_run(db, ["Rootless"], status=RunStatus.FAILED)

    control_id, text = _control_turn("undo that", previous_run_id=run_id)

    assert "Undone." in text
    assert task.id in _tasks_by_id(db)
    assert len(runs.load_history(control_id, ACTOR_ID)) == 2


@pytest.mark.parametrize("continuity", ["missing", "foreign", "incomplete"])
def test_an_unusable_continuity_cursor_degrades_and_never_blocks_undo(db, continuity):
    """A history problem must not become a mutation refusal.

    On the ordinary model path an unusable continuity locator is correctly a
    refusal, because the turn it would seed is the whole request. Here the
    request is a mutation the user is entitled to, and the history predecessor
    is a nicety. So an unusable one degrades to a root control turn.

    The three cases are the three ways `INSERT_RUN_INHERITING_HISTORY` matches no
    row, and all of them used to surface as a 403 that ate the undo.
    """
    run_id, [task] = _deleting_run(db, ["Entitled"], status=RunStatus.FAILED)

    if continuity == "missing":
        continuity_run_id = MISSING_RUN_ID
    elif continuity == "foreign":
        continuity_run_id = _run(OTHER_ACTOR_ID).id
    else:
        continuity_run_id = _run(status=RunStatus.FAILED).id

    control_id, text = _control_turn(
        "undo that", previous_run_id=run_id, continuity_run_id=continuity_run_id
    )

    assert "Undone." in text
    assert task.id in _tasks_by_id(db)
    # Root history: the two synthetic control messages and nothing inherited.
    assert len(runs.load_history(control_id, ACTOR_ID)) == 2


# --------------------------------------------------- the transport boundary


def test_a_control_failure_is_stated_in_the_protocol(db, monkeypatch):
    """RUN_ERROR, not a truncated stream. Found by neutral review.

    The model path reaches AG-UI through `transform_stream`, which converts a
    raised exception into a protocol error event. The control path does not: it
    hands already-protocol-level events to `streaming_response`, whose
    `encode_stream` in pinned 2.27.0 is a bare `async for` that encodes what it
    is given. Without the boundary in `_control_events`, an exception aborts the
    SSE body after RUN_STARTED and the browser is left with a truncated response
    and no lifecycle event to react to.

    Both writes are made to fail, which is the harshest case: the compensation
    committed, the turn could not be recorded, and the failure could not be
    recorded either. The protocol must still say so.
    """
    run_id, [task] = _deleting_run(db, ["Transport failure"])

    def _unavailable(*args, **kwargs):
        raise RuntimeError("persistence unavailable")

    monkeypatch.setattr(runs, "complete_control_turn", _unavailable)
    monkeypatch.setattr(runs, "fail_run_if_running", _unavailable)

    response = _post("undo that", previous_run_id=run_id)
    assert response.status_code == 200

    kinds = [event["type"] for event in _events(response)]
    assert kinds == ["RUN_STARTED", "RUN_ERROR"], kinds

    error = _events(response)[1]
    assert error["code"] == "TRELLIS_CONTROL_FAILURE"
    # Generic. A protocol event is client-facing text, and the exception may
    # carry a database error; the detail belongs in the run row and the log.
    assert "persistence unavailable" not in error["message"]

    monkeypatch.undo()

    # The compensation committed, which is exactly why the message warns that
    # current task state may have changed.
    assert task.id in _tasks_by_id(db)


def test_a_successful_control_turn_never_emits_run_error(db):
    """RUN_ERROR and RUN_FINISHED are alternative terminal events, never both."""
    run_id, _ = _deleting_run(db, ["Clean finish"])

    kinds = [event["type"] for event in _events(_post("undo that", previous_run_id=run_id))]

    assert "RUN_ERROR" not in kinds
    assert kinds[-1] == "RUN_FINISHED"


# ------------------------------------------------- failure after the commit


def test_history_failure_after_the_compensation_commits(db, monkeypatch):
    """The narrow window the two-transaction design leaves open, injected.

    Compensation and the control turn's history are not one transaction, and
    D-76 does not pretend otherwise. What it does claim is what happens when the
    second half fails after the first has already committed, and that claim was
    previously reasoned rather than executed. This injects the failure.

    The contract has four parts, and the fourth is the one that makes the other
    three safe:

        1. authoritative task state stays restored
        2. the control run is FAILED and its error says the mutation committed
        3. no automatic redo is attempted, because a compensating write on an
           error path is a second uncontrolled mutation
        4. retrying the same command cannot apply a second compensation wave

    Part 4 is what makes leaving the window open acceptable. The target now
    carries a compensation wave, so `attempt_run_undo` refuses it as already
    compensated, and the user's natural response to a visible failure is safe.

    Driven through `_run_control_turn` rather than the HTTP route on purpose.
    The property under test is the ordering of the two persistence blocks, and
    routing it through a stream that raises mid-response would test Starlette's
    error semantics instead.
    """
    run_id, [task] = _deleting_run(db, ["Committed then lost"])
    created = runs.create_control_turn(ACTOR_ID, "undo that", None)

    def _unavailable(*args, **kwargs):
        raise RuntimeError("history store unavailable")

    monkeypatch.setattr(runs, "complete_control_turn", _unavailable)

    with pytest.raises(RuntimeError, match="history store unavailable"):
        agent._run_control_turn(created, "undo that", run_id)

    monkeypatch.undo()

    # 1. The compensation stands. Same identity, forward version.
    restored = _tasks_by_id(db)
    assert task.id in restored, "a committed compensation was rolled back"
    assert restored[task.id]["title"] == task.title
    assert restored[task.id]["created_at"] == task.created_at
    assert restored[task.id]["version"] == task.version + 1

    # 2. The run is failed and says so in terms a reader can act on. The board
    #    may have moved, and the browser has to know to refetch.
    control = runs.load(created.id, ACTOR_ID)
    assert control.status is RunStatus.FAILED
    assert "mutation_committed=true" in control.error
    assert "history store unavailable" in control.error

    # The turn genuinely has no history, which is why it failed rather than
    # completing with a half-written transcript.
    assert runs.load_history(created.id, ACTOR_ID) == []

    # 3 and 4. The user retries the same command. It refuses, and nothing moves.
    _, text = _control_turn("undo that", previous_run_id=run_id)

    assert "already been undone" in text
    assert task.id in _tasks_by_id(db), "the retry redid the deletion"
    assert (
        sum(1 for e in _events_for(db, run_id) if e["operation"] == "restored") == 1
    ), "the retry applied a second compensation wave"


def test_the_persistence_tail_is_one_transaction(db, monkeypatch):
    """History and completion commit together, or neither does.

    Found by neutral review. These used to be two calls on two connections, so
    a `save_history` that committed followed by a failing `set_status` left a
    FAILED run carrying a fully formed "Undone." transcript. Nothing was
    corrupted, but the note claiming a failed control turn has empty history
    was false for that ordering, and a surface rendering that run would show a
    success narrative under a failure banner.

    The fix removes the ordering rather than documenting it, so this asserts the
    property the ordering used to break: when the persistence tail fails, the
    turn has no history at all. That is what makes an empty history on a failed
    control run mean something.
    """
    created = runs.create_control_turn(ACTOR_ID, "undo that", None)
    history = to_jsonable_python(
        agent._control_history([], "undo that", "Undone. I reversed 1 task change.")
    )

    monkeypatch.setattr(
        runs.sql,
        "COMPLETE_CONTROL_TURN",
        "UPDATE agent_runs SET no_such_column = 1 WHERE id = %(run_id)s",
    )

    with pytest.raises(Exception):
        runs.complete_control_turn(created.id, history)

    monkeypatch.undo()

    # Neither half landed.
    assert runs.load_history(created.id, ACTOR_ID) == []
    assert runs.load(created.id, ACTOR_ID).status is RunStatus.RUNNING

    # And the same call succeeding writes both together.
    runs.complete_control_turn(created.id, history)
    assert len(runs.load_history(created.id, ACTOR_ID)) == 2
    assert runs.load(created.id, ACTOR_ID).status is RunStatus.COMPLETED


def test_a_failed_turn_keeps_exactly_its_creation_history(db, monkeypatch):
    """The corrected invariant. Not "empty", but "unchanged since creation".

    "A failed control turn has empty history" was only ever true of a root turn.
    A turn that inherited a completed predecessor's history is born carrying it,
    and losing that on failure would be its own defect. What must never appear is
    a partial control exchange: the user message without the response, or either
    one on a run that never reached `completed`.
    """
    predecessor = _run()
    inherited = [
        {
            "parts": [
                {
                    "content": "earlier conversation",
                    "timestamp": "2026-08-20T00:00:00Z",
                    "part_kind": "user-prompt",
                }
            ],
            "kind": "request",
        }
    ]
    runs.save_history(predecessor.id, inherited)

    monkeypatch.setattr(
        runs.sql,
        "COMPLETE_CONTROL_TURN",
        "UPDATE agent_runs SET no_such_column = 1 WHERE id = %(run_id)s",
    )

    for predecessor_id, expected in [(None, 0), (predecessor.id, 1)]:
        created = runs.create_control_turn(ACTOR_ID, "undo that", predecessor_id)
        assert len(created.message_history) == expected

        with pytest.raises(Exception):
            runs.complete_control_turn(
                created.id,
                to_jsonable_python(
                    agent._control_history(
                        created.message_history, "undo that", "Undone."
                    )
                ),
            )

        after = runs.load_history(created.id, ACTOR_ID)
        assert after == created.message_history, (
            "a failed control turn changed the history it was created with"
        )
        assert len(after) == expected
        assert runs.load(created.id, ACTOR_ID).status is RunStatus.RUNNING


def test_terminal_status_is_one_way(db):
    """A run may reach one terminal status, and nothing may rewrite it.

    Without the guard, late cleanup in an outer layer could overwrite a
    committed completion with a failure, and the run record would then
    contradict the work that actually happened.
    """
    created = runs.create_control_turn(ACTOR_ID, "undo that", None)
    history = to_jsonable_python(agent._control_history([], "undo that", "Undone."))

    runs.complete_control_turn(created.id, history)
    assert runs.load(created.id, ACTOR_ID).status is RunStatus.COMPLETED

    # Cleanup arriving late finds nothing to do, and says so by returning None.
    assert runs.fail_run_if_running(created.id, "late cleanup") is None
    assert runs.load(created.id, ACTOR_ID).status is RunStatus.COMPLETED
    assert runs.load(created.id, ACTOR_ID).error is None

    # And a second completion of an already-terminal run is refused rather than
    # silently rewriting it.
    with pytest.raises(runs.RunAlreadyTerminalError):
        runs.complete_control_turn(created.id, history)

    failed = runs.create_control_turn(ACTOR_ID, "undo that", None)
    assert runs.fail_run_if_running(failed.id, "first failure") is not None
    assert runs.fail_run_if_running(failed.id, "second failure") is None
    assert runs.load(failed.id, ACTOR_ID).error == "first failure"


def test_a_failing_failure_write_surfaces_the_original_cause(db, monkeypatch):
    """When even the FAILED marking cannot be written, report the cause.

    Found by neutral review. If writes are failing broadly, the error raised by
    the failure-marking write is a symptom and the persistence error is the
    cause. Raising the symptom would hide why the turn actually failed.

    The run is then left non-terminal, and that is recorded rather than fixed.
    It is a pre-existing property of every Trellis run, not something this path
    introduces: the model path writes terminal status the same way and can fail
    the same way. What this test pins is that the failure is legible and that
    the committed compensation is untouched.
    """
    run_id, [task] = _deleting_run(db, ["Both writes lost"])
    created = runs.create_control_turn(ACTOR_ID, "undo that", None)

    def _tail_unavailable(*args, **kwargs):
        raise RuntimeError("persistence unavailable")

    def _status_unavailable(*args, **kwargs):
        raise RuntimeError("status write also unavailable")

    monkeypatch.setattr(runs, "complete_control_turn", _tail_unavailable)
    monkeypatch.setattr(runs, "fail_run_if_running", _status_unavailable)

    with pytest.raises(RuntimeError) as raised:
        agent._run_control_turn(created, "undo that", run_id)

    # The cause propagates, not the symptom.
    assert "persistence unavailable" in str(raised.value)
    assert "status write also unavailable" not in str(raised.value)

    # But the symptom is not discarded either. It is attached as a note, so a
    # reader sees both and neither is mistaken for the other.
    notes = getattr(raised.value, "__notes__", [])
    assert any("status write also unavailable" in note for note in notes), notes

    monkeypatch.undo()

    # The compensation stands, which is the part that matters to the user.
    assert task.id in _tasks_by_id(db)
    assert (
        sum(1 for e in _events_for(db, run_id) if e["operation"] == "restored") == 1
    )

    # The run is left non-terminal. Recorded, not repaired: nothing can write a
    # status while writes are failing, and a reaper is a separate decision.
    stuck = runs.load(created.id, ACTOR_ID)
    assert stuck.status is RunStatus.RUNNING
    assert stuck.error is None

    # The consequence is bounded to one turn, and this is why it is not a
    # lockout. The browser advances `previousRunId` on every RUN_STARTED, so
    # the stuck run is only the target until any next turn issues a new id.
    _, text = _control_turn("undo that", previous_run_id=created.id)
    assert "has not finished yet" in text

    _, recovered = _control_turn("undo that", previous_run_id=run_id)
    assert "already been undone" in recovered


def test_failure_before_the_compensation_claims_no_committed_mutation(db, monkeypatch):
    """The other side of the same split, and the reason the two blocks differ.

    A failure while the undo attempt itself is running committed nothing, so the
    run must fail without claiming a mutation landed. If both blocks wrote the
    same error, `mutation_committed=true` would stop distinguishing anything and
    every genuine post-commit failure would become unreadable: the browser would
    be told the board may have moved on turns where it certainly did not.

    Injected at `attempt_run_undo`, which is the only thing between the control
    run being created and the kernel committing.
    """
    run_id, [task] = _deleting_run(db, ["Never reached"])
    created = runs.create_control_turn(ACTOR_ID, "undo that", None)

    def _unavailable(*args, **kwargs):
        raise RuntimeError("undo boundary unavailable")

    monkeypatch.setattr(runs, "attempt_run_undo", _unavailable)

    with pytest.raises(RuntimeError, match="undo boundary unavailable"):
        agent._run_control_turn(created, "undo that", run_id)

    monkeypatch.undo()

    control = runs.load(created.id, ACTOR_ID)
    assert control.status is RunStatus.FAILED
    assert "undo boundary unavailable" in control.error
    assert "mutation_committed=true" not in control.error

    # Nothing moved, which is what the error is entitled to imply.
    assert _tasks_by_id(db) == {}
    assert not any(e["operation"] == "restored" for e in _events_for(db, run_id))


def test_a_semantic_refusal_completes_rather_than_failing(db):
    """A refusal is an answer, not an error. D-76 rules only infrastructure
    failures produce a FAILED control run."""
    foreign_id, [task] = _deleting_run(db, ["Not yours"], actor_id=OTHER_ACTOR_ID)

    control_id, text = _control_turn("undo that", previous_run_id=foreign_id)

    assert text == NO_TARGET_TEXT
    control = runs.load(control_id, ACTOR_ID)
    assert control.status is RunStatus.COMPLETED
    assert control.error is None
    assert task.id not in _tasks_by_id(db)


# ---------------------------------------------------------- trust boundary


def test_the_locators_never_reach_the_rebuilt_run_input():
    """Both are extracted and both are discarded before any adapter is built."""
    rebuilt = agent._accepted_run_input(uuid4(), "ordinary message")
    assert rebuilt.forwarded_props == {}

    control = agent._control_run_input(uuid4())
    assert control.forwarded_props == {}
    assert control.messages == []
    assert control.tools == []


def test_run_detail_and_the_authoritative_attempt_share_one_predicate(db):
    """One definition of undoability, read by the projection and by the mutation.

    If these ever disagree, a surface offers an undo the authoritative path
    refuses, or hides one it would have allowed.
    """
    run_id, _ = _deleting_run(db, ["Shared"])

    assert runs.detail(run_id, ACTOR_ID).can_undo is True
    attempt = runs.attempt_run_undo(run_id, ACTOR_ID)
    assert attempt.ineligible is None
    assert attempt.applied == 1

    assert runs.detail(run_id, ACTOR_ID).can_undo is False
    assert (
        runs.attempt_run_undo(run_id, ACTOR_ID).ineligible
        is runs.UndoIneligibility.ALREADY_COMPENSATED
    )


def test_attempt_run_undo_refuses_a_foreign_run(db):
    """Missing and foreign are one refusal at the authoritative boundary too."""
    from app.errors import OutOfScopeError

    foreign_id, _ = _deleting_run(db, ["Theirs"], actor_id=OTHER_ACTOR_ID)

    with pytest.raises(OutOfScopeError):
        runs.attempt_run_undo(foreign_id, ACTOR_ID)
    with pytest.raises(OutOfScopeError):
        runs.attempt_run_undo(MISSING_RUN_ID, ACTOR_ID)


def test_an_updating_run_is_undone_to_its_prior_field_values(db):
    """Undo is not delete-specific. An update wave reverses field by field."""
    setup = _run()
    task = _create_task(db, setup.id, "Original", notes="before")

    run_id = runs.create(ACTOR_ID, "edit it", "d76-fixture-model").id
    mutation = domain.update_task(
        ACTOR_ID,
        UpdateTaskArgs(
            task_id=task.id,
            expected_version=task.version,
            title="Edited",
            notes="after",
        ),
        conn=db,
    )
    domain.write_events(run_id, ACTOR_ID, mutation.events, conn=db)
    db.commit()
    runs.set_status(run_id, RunStatus.COMPLETED)

    _, text = _control_turn("revert that", previous_run_id=run_id)

    assert "Undone." in text
    row = _tasks_by_id(db)[task.id]
    assert row["title"] == "Original"
    assert row["notes"] == "before"
    assert row["version"] == task.version + 2
