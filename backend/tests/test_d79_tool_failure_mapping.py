"""D-79 hardening. Expected refusals stay inside the Pydantic tool protocol.

Found by a live dependency-chain test rather than by any gate. The model set up
a blocking chain, reused an `expected_version` it remembered from earlier in the
turn, and `update_task` refused it. The refusal was correct. Where it went was
not: an ordinary application exception is neither `ModelRetry` nor `ToolFailed`,
so Pydantic AI let it out of the tool protocol, and it aborted the whole agent
run and the response stream with it.

That is the wrong outcome for a stale version, which is a routine thing for a
model to send and a recoverable one. The user's request may still be valid; only
the concurrency token is out of date.

Nothing about the refusal changes. `domain.update_task` still compares the
locked version and still raises, direct callers still see Trellis exceptions,
and the guarded UPDATE keeps its predicate. Only the model-facing adapter
changes, because that is the one place that knows it is talking to a model:

    VERSION_CONFLICT      -> ModelRetry, because the next action is known:
                             look the task up again and use what it returns.
    EXTERNAL_DIVERGENCE   -> ToolFailed, because no correction to the arguments
                             makes the mutation safe, so another attempt would
                             only earn the same refusal.
    OUT_OF_SCOPE          -> ToolFailed, because retrying the same identifier
                             cannot make it usable.

The last one is deliberately generic in what it tells the model. A missing task
and another actor's task are the same refusal by design, and policy raises it
before the divergence check to keep them that way, so a message that named which
one had happened would hand the model an oracle for which task ids exist.

The translation applies at every model-facing tool wrapper, not only
`update_task`, because every tool reaches `policy.check` and can be refused this
way.

Anything else keeps escaping. An unexpected exception is supposed to be
unexpected, and turning a bug or an outage into a cheerful "try again" would
hide exactly the failures worth seeing.
"""

import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent as agent_module
from app import domain, runs, sql
from app.db import pool
from app.errors import ExternalDivergenceError, VersionConflictError
from app.models import CreateTaskArgs, ToolName, UpdateTaskArgs

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")


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


_MARK_DIVERGED = """
INSERT INTO linear_task_state (task_id, external_id, external_updated_at, diverged)
VALUES (%(task_id)s, %(external_id)s, now(), true)
ON CONFLICT (task_id) DO UPDATE SET diverged = EXCLUDED.diverged;
"""


def _run(actor_id=ACTOR_ID):
    return runs.create(actor_id, "d79 mapping fixture", "d79-mapping-model").id


def _task(conn, run_id, title="Run the farm", **fields):
    mutation = domain.create_task(ACTOR_ID, CreateTaskArgs(title=title, **fields), conn=conn)
    domain.write_events(run_id, ACTOR_ID, mutation.events, conn=conn)
    conn.commit()
    return mutation.tasks[0]


def _row(conn, task_id):
    return conn.execute(
        "SELECT * FROM tasks WHERE id = %(id)s", {"id": task_id}
    ).fetchone()


def _events_for(conn, task_id):
    return conn.execute(
        "SELECT * FROM task_events WHERE task_id = %(id)s ORDER BY id",
        {"id": task_id},
    ).fetchall()


def _parts(messages, kind):
    return [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, kind)
    ]


# ------------------------------------------------ the domain is unchanged


def test_the_domain_still_raises_the_trellis_refusal(db):
    """The translation is at the adapter, so direct callers must be unaffected.

    If this ever starts returning instead of raising, the guard has been moved
    or weakened, and every deterministic caller that relies on the exception,
    undo included, is now reading a different contract.
    """
    run_id = _run()
    task = _task(db, run_id)

    with pytest.raises(VersionConflictError):
        domain.update_task(
            ACTOR_ID,
            UpdateTaskArgs(
                task_id=task.id,
                expected_version=task.version + 5,
                priority="high",
            ),
            conn=db,
        )
    db.rollback()
    assert _row(db, task.id)["version"] == task.version


# ------------------------------------------- version conflict is a retry


def test_a_stale_version_retries_instead_of_aborting_the_run(db):
    """The whole failure, end to end, through the real agent.

    Before this mapping the run raised out of `run_sync` on the first model
    turn. The assertion that matters most is not that a retry part exists, it is
    that the run reached a second turn at all.
    """
    run_id = _run()
    task = _task(db, run_id)

    seen: dict[str, object] = {}
    state = {"turn": 0}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        state["turn"] += 1
        if state["turn"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        ToolName.UPDATE_TASK.value,
                        {
                            "task_id": str(task.id),
                            "expected_version": task.version + 5,
                            "priority": "high",
                        },
                    )
                ]
            )
        seen["retries"] = _parts(messages, RetryPromptPart)
        return ModelResponse(parts=[TextPart("I will look the task up again.")])

    built = agent_module.build_agent(FunctionModel(model))
    result = built.run_sync(
        "set it to high priority",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
    )

    assert state["turn"] == 2, "the run did not survive the refusal"
    assert result.output

    retries = seen["retries"]
    assert retries, "the refusal produced no retry prompt"
    assert any(part.tool_name == ToolName.UPDATE_TASK.value for part in retries)

    detail = " ".join(str(part.content) for part in retries).lower()
    assert "resolve the task reference again" in detail
    assert "do not" in detail and "guess" in detail, detail
    assert str(task.version) not in detail, (
        "the retry must not hand back a version; the resolver owns that"
    )

    # Nothing committed on the refused attempt.
    assert _row(db, task.id)["version"] == task.version
    assert _row(db, task.id)["priority"] == task.priority.value
    assert len(_events_for(db, task.id)) == 1, "created event only"


def test_the_model_can_refresh_and_then_succeed(db):
    """The recovery the retry is supposed to enable, driven all the way through.

    A retry the model cannot act on is not a fix. This walks the intended loop:
    stale call, refusal, authoritative resolve, corrected call, one commit.
    """
    run_id = _run()
    task = _task(db, run_id)

    calls: list[str] = []
    state = {"turn": 0, "resolved_version": None}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        state["turn"] += 1
        if state["turn"] == 1:
            calls.append("stale_update")
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        ToolName.UPDATE_TASK.value,
                        {
                            "task_id": str(task.id),
                            "expected_version": task.version + 5,
                            "priority": "high",
                        },
                    )
                ]
            )
        if state["turn"] == 2:
            assert _parts(messages, RetryPromptPart), "no retry to react to"
            calls.append("resolve")
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        ToolName.RESOLVE_TASK_REFERENCE.value,
                        {"reference": "Run the farm"},
                    )
                ]
            )
        if state["turn"] == 3:
            # Read the version off the authoritative resolver result, which is
            # exactly what the retry message told the model to do.
            returns = _parts(messages, ToolReturnPart)
            resolved = next(
                part.content
                for part in returns
                if part.tool_name == ToolName.RESOLVE_TASK_REFERENCE.value
            )
            version = resolved["resolved"]["current_version"]
            state["resolved_version"] = version
            calls.append("fresh_update")
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        ToolName.UPDATE_TASK.value,
                        {
                            "task_id": str(task.id),
                            "expected_version": version,
                            "priority": "high",
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    built = agent_module.build_agent(FunctionModel(model))
    built.run_sync(
        "set it to high priority",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
    )

    assert calls == ["stale_update", "resolve", "fresh_update"]
    assert state["resolved_version"] == task.version

    row = _row(db, task.id)
    assert row["priority"] == "high"
    assert row["version"] == task.version + 1, "exactly one mutation committed"
    assert len(_events_for(db, task.id)) == 2, "created plus one update"


def test_the_refused_attempt_does_not_mark_the_run_as_having_mutated(db):
    """`mutation_committed` gates later behaviour, so a refusal must not set it.

    The flag is written after the tool body returns. A translation placed above
    the assignment keeps that true; one placed below would report a mutation
    that never happened.
    """
    run_id = _run()
    task = _task(db, run_id)

    effects = agent_module.RunEffects()
    state = {"turn": 0}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        state["turn"] += 1
        if state["turn"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        ToolName.UPDATE_TASK.value,
                        {
                            "task_id": str(task.id),
                            "expected_version": task.version + 5,
                            "priority": "high",
                        },
                    )
                ]
            )
        assert effects.mutation_committed is False, (
            "a refused mutation was recorded as committed"
        )
        return ModelResponse(parts=[TextPart("done")])

    built = agent_module.build_agent(FunctionModel(model))
    built.run_sync(
        "go",
        deps=agent_module.TrellisDeps(
            actor_id=ACTOR_ID, run_id=run_id, effects=effects
        ),
    )
    assert effects.mutation_committed is False


# --------------------------------------------- divergence is terminal


def test_a_diverged_task_fails_the_call_rather_than_inviting_a_retry(db):
    """Divergence is not the model's mistake, so it must not read as one.

    A retry here would be a loop: the arguments are fine, and the next attempt
    refuses identically. `ToolFailed` gives the model a definitively failed call
    it can explain instead.
    """
    run_id = _run()
    task = _task(db, run_id)
    db.execute(_MARK_DIVERGED, {"task_id": task.id, "external_id": "TRE-1"})
    db.commit()

    seen: dict[str, object] = {}
    state = {"turn": 0}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        state["turn"] += 1
        if state["turn"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        ToolName.UPDATE_TASK.value,
                        {
                            "task_id": str(task.id),
                            "expected_version": task.version,
                            "priority": "high",
                        },
                    )
                ]
            )
        seen["retries"] = _parts(messages, RetryPromptPart)
        seen["returns"] = _parts(messages, ToolReturnPart)
        return ModelResponse(parts=[TextPart("That task is out of sync.")])

    built = agent_module.build_agent(FunctionModel(model))
    built.run_sync(
        "set it to high priority",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
    )

    assert state["turn"] == 2, "the run did not survive the refusal"
    assert seen["retries"] == [], "divergence must not be offered as a retry"

    returns = [
        part
        for part in seen["returns"]
        if part.tool_name == ToolName.UPDATE_TASK.value
    ]
    assert returns, "the model received no result for the failed call"
    reported = " ".join(str(part.content) for part in returns).lower()
    assert "diverged" in reported, reported

    assert _row(db, task.id)["version"] == task.version
    assert len(_events_for(db, task.id)) == 1, "created event only"


def test_the_divergence_refusal_still_raises_for_direct_callers(db):
    """Same boundary rule as the version conflict: only the adapter translates."""
    from app import policy

    run_id = _run()
    task = _task(db, run_id)
    db.execute(_MARK_DIVERGED, {"task_id": task.id, "external_id": "TRE-2"})
    db.commit()

    with pytest.raises(ExternalDivergenceError):
        policy._refuse_if_diverged([task.id])


# ------------------------------------------ out of scope is terminal


def _failed_update_report(db, run_id, arguments):
    """Drive one update_task call and return what the model was told."""
    seen: dict[str, object] = {}
    state = {"turn": 0}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        state["turn"] += 1
        if state["turn"] == 1:
            return ModelResponse(
                parts=[ToolCallPart(ToolName.UPDATE_TASK.value, arguments)]
            )
        seen["retries"] = _parts(messages, RetryPromptPart)
        seen["returns"] = [
            part
            for part in _parts(messages, ToolReturnPart)
            if part.tool_name == ToolName.UPDATE_TASK.value
        ]
        return ModelResponse(parts=[TextPart("I could not use that task.")])

    built = agent_module.build_agent(FunctionModel(model))
    built.run_sync(
        "change it",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
    )
    seen["turns"] = state["turn"]
    return seen


def test_a_missing_task_fails_the_call_without_aborting_the_run(db):
    """Retrying the same identifier cannot make it resolvable, so it is terminal."""
    run_id = _run()
    seen = _failed_update_report(
        db,
        run_id,
        {"task_id": str(uuid4()), "expected_version": 1, "priority": "high"},
    )

    assert seen["turns"] == 2, "the run did not survive the refusal"
    assert seen["retries"] == [], "an unusable reference must not burn retry budget"
    assert seen["returns"], "the model received no result for the failed call"


def test_a_missing_and_a_foreign_task_are_reported_identically(db):
    """The non-enumeration boundary has to survive the new message.

    `OutOfScopeError` covers both on purpose, and policy raises it before the
    divergence check for the same reason. A model-facing message that said
    "no such task" or "not yours" would hand the model an oracle for which task
    ids exist, which is precisely what the shared refusal exists to prevent.
    """
    run_id = _run()
    other_run = runs.create(
        UUID("00000000-0000-0000-0000-000000000002"), "other", "m"
    ).id
    foreign = domain.create_task(
        UUID("00000000-0000-0000-0000-000000000002"),
        CreateTaskArgs(title="Theirs"),
        conn=db,
    )
    domain.write_events(
        other_run, UUID("00000000-0000-0000-0000-000000000002"), foreign.events, conn=db
    )
    db.commit()

    missing = _failed_update_report(
        db,
        run_id,
        {"task_id": str(uuid4()), "expected_version": 1, "priority": "high"},
    )
    theirs = _failed_update_report(
        db,
        _run(),
        {
            "task_id": str(foreign.tasks[0].id),
            "expected_version": foreign.tasks[0].version,
            "priority": "high",
        },
    )

    def reported(seen):
        return " ".join(str(part.content) for part in seen["returns"])

    assert reported(missing) == reported(theirs), (
        "a missing task and a foreign task told the model different things"
    )
    message = reported(missing).lower()
    assert "not available" in message
    for leak in ("does not exist", "no such task", "not found", "own", "another"):
        assert leak not in message, f"the refusal leaked {leak!r}: {message}"


# ------------------------------------- everything else still escapes


def test_an_unexpected_exception_is_not_turned_into_a_retry(db):
    """The mapping must stay narrow, and this is what keeps it honest.

    A broad `except Exception` would convert bugs, outages, and serialization
    failures into a polite suggestion that the model try again, which hides the
    failures most worth seeing and can loop on them. Only refusals whose next
    action is known are translated.
    """
    run_id = _run()
    task = _task(db, run_id)

    boom = RuntimeError("database is on fire")

    def explode(ctx, arguments):
        raise boom

    state = {"turn": 0}

    async def model(messages: list[ModelMessage], info: AgentInfo):
        state["turn"] += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    ToolName.UPDATE_TASK.value,
                    {
                        "task_id": str(task.id),
                        "expected_version": task.version,
                        "priority": "high",
                    },
                )
            ]
        )

    import app.tools as tools_module

    original = tools_module.update_task
    tools_module.update_task = explode
    try:
        built = agent_module.build_agent(FunctionModel(model))
        with pytest.raises(RuntimeError, match="database is on fire"):
            built.run_sync(
                "go",
                deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=run_id),
            )
    finally:
        tools_module.update_task = original

    assert state["turn"] == 1, "an unexpected error must not be retried"


def test_the_translation_preserves_the_original_cause(db):
    """`raise ... from exc` keeps the Trellis reason available to logs.

    The model sees a chosen sentence; whoever debugs this later needs the
    exception that actually happened, not a paraphrase of it.
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(agent_module._model_facing_refusals))
    tree = ast.parse(source)
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
    ]

    caught = {ast.unparse(handler.type) for handler in handlers}
    assert caught == {
        "VersionConflictError",
        "ExternalDivergenceError",
        "OutOfScopeError",
    }, f"the translated set changed: {caught}"

    # Narrowness is half the contract: a broad catch here would convert bugs and
    # outages into a polite suggestion that the model try again.
    assert "PolicyError" not in caught
    assert "Exception" not in caught

    for handler in handlers:
        raises = [
            node for node in ast.walk(handler) if isinstance(node, ast.Raise)
        ]
        assert raises, f"{ast.unparse(handler.type)} swallows the refusal"
        for node in raises:
            assert node.cause is not None, (
                f"{ast.unparse(handler.type)} discards the original cause; "
                "use `raise ... from exc`"
            )


# ---------------------------------------------------- the prompt rule


def test_the_prompt_forbids_reusing_a_remembered_version():
    """The trace that started this showed a version reused "per earlier" state.

    The prompt already said to take `expected_version` from the authoritative
    lookup, but never said when that lookup had to have happened, so a version
    read earlier in the same turn still satisfied it as written.
    """
    from app import prompts

    text = prompts.SYSTEM_PROMPT.lower()
    assert "ephemeral concurrency token" in text
    assert "never reuse an expected_version" in text
    assert "resolve it again immediately before the mutation" in text
    assert "never increment or guess the version" in text
