"""D-78. Deterministic single-task note append.

Natural language distinguishes replacing notes from adding to them, but the
typed mutation contract only ever exposed replacement. To honour "add a note",
the model had to fetch the current notes, join them from memory, and send the
whole value back. That makes the model the temporary owner of authoritative
state, which is the one thing this system exists not to do: the value it echoes
back is a value it read at some earlier point, and anything that changed in
between is silently overwritten.

So the merge moves into deterministic code, behind the row lock the mutation
already takes. The model sends only its new fragment.

The tests below are ordered as the contract reads: what the schema refuses,
what the merge produces, what the transformation must not disturb, and what a
replay must not do twice.
"""

import sys
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import domain, runs, sql, tools
from app.db import pool
from app.errors import ValidationFailedError, VersionConflictError
from app.limits import TASK_NOTES_MAX_CHARS
from app.models import (
    BulkUpdateTasksArgs,
    CreateTaskArgs,
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


# --------------------------------------------------------------- fixtures


def _run(actor_id=ACTOR_ID):
    """The server-issued run id. `runs.create` returns the whole row."""
    return runs.create(actor_id, "d78 fixture", "d78-fixture-model").id


def _task(conn, run_id, title="Run the farm", actor_id=ACTOR_ID, **fields):
    mutation = domain.create_task(
        actor_id, CreateTaskArgs(title=title, **fields), conn=conn
    )
    domain.write_events(run_id, actor_id, mutation.events, conn=conn)
    conn.commit()
    return mutation.tasks[0]


def _append(conn, task, fragment, **extra):
    """One domain-level append against the locked authoritative row."""
    return domain.update_task(
        ACTOR_ID,
        UpdateTaskArgs(
            task_id=task.id,
            expected_version=task.version,
            append_notes=fragment,
            **extra,
        ),
        conn=conn,
    )


def _notes(conn, task_id):
    row = conn.execute(
        "SELECT notes FROM tasks WHERE id = %(id)s", {"id": task_id}
    ).fetchone()
    return None if row is None else row["notes"]


def _append_through_the_tool(run_id, task, fragment, *, tool_call_id):
    ctx = tools.ToolContext(
        actor_id=ACTOR_ID,
        run_id=run_id,
        tool_call_id=tool_call_id,
        tool_call_approved=False,
    )
    return tools.update_task(
        ctx,
        UpdateTaskArgs(
            task_id=task.id,
            expected_version=task.version,
            append_notes=fragment,
        ),
    )


# ------------------------------------------------ what the schema refuses


def test_append_is_absent_from_the_bulk_schema():
    """Bulk append is not authorized, and inheritance is how it would leak.

    `UpdateTaskArgs` and `BulkUpdateTasksArgs` share `MutableTaskFields`. A
    field added to that base would appear on both, and the model would be shown
    a bulk append this decision never specified: no per-row merge, no per-row
    final-size check, no set-based semantics.
    """
    assert "append_notes" in UpdateTaskArgs.model_fields
    assert "append_notes" not in BulkUpdateTasksArgs.model_fields

    with pytest.raises(ValidationError):
        BulkUpdateTasksArgs(task_ids=[uuid4()], append_notes="nope")


def test_notes_and_append_notes_together_are_refused():
    with pytest.raises(ValidationError):
        UpdateTaskArgs(
            task_id=uuid4(),
            expected_version=1,
            notes="replace",
            append_notes="add",
        )


def test_an_explicitly_null_notes_still_counts_as_mentioning_notes():
    """`notes=None` is still a caller who named the field, so the pair is refused.

    Reading this as "notes was not really sent" would make the refusal depend on
    a value rather than on the request's shape, and the model would learn that
    one of the two ways of sending both fields happens to work.
    """
    with pytest.raises(ValidationError):
        UpdateTaskArgs(
            task_id=uuid4(),
            expected_version=1,
            notes=None,
            append_notes="add",
        )


def test_an_empty_append_is_refused():
    with pytest.raises(ValidationError):
        UpdateTaskArgs(task_id=uuid4(), expected_version=1, append_notes="")


def test_replacement_and_clearing_are_unchanged(db):
    run_id = _run()
    task = _task(db, run_id, notes="original")

    replaced = domain.update_task(
        ACTOR_ID,
        UpdateTaskArgs(
            task_id=task.id, expected_version=task.version, notes="replacement"
        ),
        conn=db,
    )
    assert replaced.tasks[0].notes == "replacement"

    cleared = domain.update_task(
        ACTOR_ID,
        UpdateTaskArgs(
            task_id=task.id,
            expected_version=replaced.tasks[0].version,
            notes="",
        ),
        conn=db,
    )
    assert cleared.tasks[0].notes == ""


def test_the_prompt_routes_replace_and_append_without_contradiction():
    """Two rules must not both claim the same request.

    Rule 5 originally said "if the user asks only to change notes, send notes".
    "Change" covers appending, so it contradicted rule 5a, which routes
    add/append to `append_notes`. The backend is correct either way, but an
    ambiguous instruction is how a model picks the wrong valid field, and the
    wrong valid field here silently replaces a note the user meant to extend.
    """
    from app.prompts import SYSTEM_PROMPT

    assert "asks only to change notes" not in SYSTEM_PROMPT
    assert "asks only to REPLACE the notes" in SYSTEM_PROMPT
    assert "asks only to ADD to the notes" in SYSTEM_PROMPT

    # Both verbs map to a field, and the mapping appears before the worked
    # example that shows not reading the notes back.
    assert "add, append, add to, add a note    -> append_notes" in SYSTEM_PROMPT
    assert "set, replace, overwrite, rewrite   -> notes" in SYSTEM_PROMPT


def test_no_rule_anywhere_tells_the_model_to_join_notes_itself():
    """One rule saying the right thing does not help if another says otherwise.

    A blind review found that rule 9 still instructed the model to "preserve the
    existing notes and append the requested content" and to "use authoritative
    tool data" when appending, with a worked example of hand-assembling the
    joined value. That is the exact mechanism D-78 removes, and it sat two
    paragraphs below the new rule that forbids it.

    The earlier prompt test could not see this: it asserted that the new strings
    were present and that one old phrase was absent, which says nothing about a
    different rule elsewhere in the prompt saying the opposite. This test asks
    the question the other one could not, by naming the instruction rather than
    the location.
    """
    from app.prompts import SYSTEM_PROMPT

    forbidden = (
        # The model deciding it needs the current notes in order to append.
        "If appending requires knowing the current notes",
        # Any instruction that makes the model the one doing the preserving.
        "preserve the existing\n   notes and append the requested content",
    )
    present = [phrase for phrase in forbidden if phrase in SYSTEM_PROMPT]
    assert not present, f"the prompt still tells the model to join notes: {present}"

    # The server is named as the thing that preserves, so the reader cannot come
    # away thinking it is their job.
    assert "The server will preserve the existing notes" in SYSTEM_PROMPT
    assert "Never read the current notes in order to join them yourself" in (
        SYSTEM_PROMPT
    )


def test_the_model_facing_schema_carries_the_append_contract():
    """Assert the definition the model is actually sent, not just the class.

    `UpdateTaskArgs.model_fields` proves what Python holds. It does not prove
    what Pydantic AI generates and hands to the provider, and those are the two
    places this contract has to agree. A field present on the class but absent
    from the generated schema would leave the model unable to name it while
    every class-level assertion stayed green.

    `TestModel(call_tools=[])` drives one agent construction with no provider
    request and no tool execution, so this needs neither a credential nor a
    database. `conftest` additionally sets `ALLOW_MODEL_REQUESTS` False for any
    test without the `network` marker, so a live request cannot happen here.
    """
    from pydantic_ai import models as pydantic_ai_models
    from pydantic_ai.models.test import TestModel

    from app import agent as agent_module

    assert pydantic_ai_models.ALLOW_MODEL_REQUESTS is False

    test_model = TestModel(call_tools=[])
    built = agent_module.build_agent(test_model)
    built.run_sync(
        "hello",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=uuid4()),
    )

    definitions = {
        tool.name: tool
        for tool in test_model.last_model_request_parameters.function_tools
    }

    update = definitions["update_task"]
    update_properties = update.parameters_json_schema["properties"]
    assert "append_notes" in update_properties
    assert "notes" in update_properties

    # The distinction is carried by the tool description, because the generated
    # JSON schema for the field holds its constraints rather than its meaning.
    description = update.description or ""
    assert "append_notes" in description
    assert "replaces the whole note value" in description
    assert "ONLY the new text" in description

    # The bulk tool must not have inherited an append.
    bulk = definitions["bulk_update_tasks"]
    assert "append_notes" not in bulk.parameters_json_schema["properties"]

    # And the fragment keeps its bounds on the way to the model, so an empty
    # append is refused by the schema rather than by a later check.
    append_schema = update_properties["append_notes"]
    constraint = next(
        option
        for option in append_schema["anyOf"]
        if option.get("type") == "string"
    )
    assert constraint["minLength"] == 1
    assert constraint["maxLength"] == TASK_NOTES_MAX_CHARS

    # The field carries its own meaning, not only its bounds. A model reading
    # the parameter schema alone must still learn not to send back the notes it
    # already has. The description sits on the string branch of the anyOf that
    # the optional field generates, not at the property's top level.
    assert "Only the new note text" in constraint["description"]
    assert "Do not include" in constraint["description"]


def test_the_displayed_profile_chain_holds_end_to_end():
    """Close the gap between the enum the header parses and the real profile.

    The frontend gate reads the `ToolName` enum out of source text, but the
    authority for what the browser agent can actually do is `ALL_TOOLS`, and the
    authority for what the model is offered is the generated function tools.
    Those three agree today only because `ALL_TOOLS` is defined as every
    `ToolName`.

        header labels  <-> ToolName          the frontend cross-boundary test
        ToolName       <-> ALL_TOOLS         asserted here
        ALL_TOOLS      <-> function_tools    asserted here

    Without the two links below, a refactor that made `ALL_TOOLS` narrower than
    `ToolName` would leave the header advertising capabilities the browser agent
    no longer has, with every existing gate still green.
    """
    from pydantic_ai.models.test import TestModel

    from app import agent as agent_module
    from app.models import ToolName

    test_model = TestModel(call_tools=[])
    built = agent_module.build_agent(test_model)
    built.run_sync(
        "hello",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=uuid4()),
    )

    offered = {
        tool.name for tool in test_model.last_model_request_parameters.function_tools
    }

    assert agent_module.ALL_TOOLS == frozenset(name.value for name in ToolName)
    assert offered == set(agent_module.ALL_TOOLS)


# ------------------------------------------------- what the merge produces


@pytest.mark.parametrize(
    "existing,addition,expected",
    [
        ("alpha", "beta", "alpha\nbeta"),
        ("", "beta", "beta"),
        ("alpha\n", "beta", "alpha\nbeta"),
        ("alpha", "beta\ngamma", "alpha\nbeta\ngamma"),
        # A leading newline the caller supplied is content, not a separator.
        # Collapsing it would silently edit their text.
        ("alpha", "\nbeta", "alpha\n\nbeta"),
        ("alpha\n\n", "beta", "alpha\n\nbeta"),
    ],
)
def test_the_separator_rule(existing, addition, expected):
    assert domain.merge_appended_notes(existing, addition) == expected


def test_the_fragment_is_preserved_exactly():
    """No bullet, number, punctuation, or blank line is invented."""
    fragment = "  - 2 x 3 = 6; check?  "
    merged = domain.merge_appended_notes("alpha", fragment)
    assert merged == "alpha\n" + fragment
    assert merged.endswith(fragment)


def test_an_append_to_empty_notes(db):
    run_id = _run()
    task = _task(db, run_id)
    assert task.notes == ""

    result = _append(db, task, "Feed the cows.")

    assert result.tasks[0].notes == "Feed the cows."
    assert _notes(db, task.id) == "Feed the cows."


def test_an_append_to_existing_notes(db):
    run_id = _run()
    task = _task(db, run_id, notes="Feed the cows.")

    result = _append(db, task, "Check the coop.")

    assert result.tasks[0].notes == "Feed the cows.\nCheck the coop."


def test_a_caller_supplied_leading_newline_survives_the_whole_path(db):
    """The blank line the caller asked for is content, and reaches the row.

    This exists as a separate database-level case because the merge function's
    own unit test cannot protect it. A mutation that normalizes the fragment in
    `_effective_update`, before the merge is ever called, leaves
    `merge_appended_notes` correct and every parametrized case passing while
    still editing the caller's text on the way to PostgreSQL.
    """
    run_id = _run()
    task = _task(db, run_id, notes="alpha")

    result = _append(db, task, "\nbeta")

    assert result.tasks[0].notes == "alpha\n\nbeta"
    assert _notes(db, task.id) == "alpha\n\nbeta"


def test_a_fragment_of_only_whitespace_is_still_the_callers_text(db):
    """Whitespace is not nothing. `min_length` refuses empty, not blank."""
    run_id = _run()
    task = _task(db, run_id, notes="alpha")

    result = _append(db, task, "   ")

    assert result.tasks[0].notes == "alpha\n   "


def test_sequential_appends_accumulate(db):
    run_id = _run()
    task = _task(db, run_id, notes="one")

    second = _append(db, task, "two").tasks[0]
    third = _append(db, second, "three").tasks[0]

    assert third.notes == "one\ntwo\nthree"
    assert third.version == task.version + 2


def test_the_merge_reads_locked_state_not_a_caller_snapshot(db):
    """The authoritative value is the row's, not anything the caller remembers.

    A model that had read the notes earlier would append to what it saw. This
    appends to what is actually stored at merge time.
    """
    run_id = _run()
    task = _task(db, run_id, notes="stale")

    moved = domain.update_task(
        ACTOR_ID,
        UpdateTaskArgs(
            task_id=task.id, expected_version=task.version, notes="current"
        ),
        conn=db,
    ).tasks[0]

    result = _append(db, moved, "added")

    assert result.tasks[0].notes == "current\nadded"
    assert "stale" not in result.tasks[0].notes


# -------------------------------------------------------- the size ceiling


def test_the_merged_size_is_what_is_validated(db):
    """A legal fragment can still produce an illegal note."""
    run_id = _run()
    task = _task(db, run_id, notes="x" * (TASK_NOTES_MAX_CHARS - 1))

    # The fragment alone is well within the per-field ceiling.
    fragment = "yy"
    assert len(fragment) <= TASK_NOTES_MAX_CHARS

    with pytest.raises(ValidationFailedError):
        _append(db, task, fragment)


def test_an_overflowing_append_mutates_nothing(db):
    run_id = _run()
    original = "x" * (TASK_NOTES_MAX_CHARS - 1)
    task = _task(db, run_id, notes=original)

    with pytest.raises(ValidationFailedError):
        _append(db, task, "yy")

    db.rollback()

    row = db.execute(
        "SELECT notes, version FROM tasks WHERE id = %(id)s", {"id": task.id}
    ).fetchone()
    assert row["notes"] == original
    assert row["version"] == task.version

    events = db.execute(
        "SELECT count(*) AS n FROM task_events WHERE task_id = %(id)s",
        {"id": task.id},
    ).fetchone()
    assert events["n"] == 1  # the creation event only


def test_a_merge_landing_exactly_on_the_ceiling_is_allowed(db):
    """The refusal is over the limit, not at it."""
    run_id = _run()
    task = _task(db, run_id, notes="x" * (TASK_NOTES_MAX_CHARS - 2))

    result = _append(db, task, "y")

    assert len(result.tasks[0].notes) == TASK_NOTES_MAX_CHARS


# ----------------------------------- what the transformation must not break


def test_an_append_leaves_omitted_fields_omitted(db):
    """The omitted-versus-null contract survives the rewrite.

    `_update_parameters` decides whether to clear `due_date` and `blocked_by`
    from `model_fields_set`. Rebuilding the arguments through a full
    `model_dump` would mark every field as set, and an append that never
    mentioned `due_date` would clear it.
    """
    run_id = _run()
    other = _task(db, run_id, title="Blocker")
    task = _task(
        db,
        run_id,
        notes="keep",
        due_date=date(2026, 9, 1),
        blocked_by=other.id,
    )

    result = _append(db, task, "added")

    after = result.tasks[0]
    assert after.notes == "keep\nadded"
    assert after.due_date == date(2026, 9, 1)
    assert after.blocked_by == other.id


def test_the_effective_arguments_keep_the_original_fields_set():
    """Directly assert the seam, not only its observable effect."""
    arguments = UpdateTaskArgs(
        task_id=uuid4(), expected_version=1, append_notes="added"
    )
    assert "due_date" not in arguments.model_fields_set
    assert "blocked_by" not in arguments.model_fields_set
    assert "notes" not in arguments.model_fields_set

    before = domain.Task(
        id=arguments.task_id,
        owner_id=ACTOR_ID,
        title="Run the farm",
        notes="existing",
        version=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    effective = domain._effective_update(arguments, before)

    assert effective.notes == "existing\nadded"
    assert "notes" in effective.model_fields_set
    assert "due_date" not in effective.model_fields_set
    assert "blocked_by" not in effective.model_fields_set


def test_an_explicit_null_due_date_still_clears_alongside_an_append(db):
    """Explicit null keeps meaning null. The append does not suppress it."""
    run_id = _run()
    task = _task(db, run_id, notes="keep", due_date=date(2026, 9, 1))

    result = _append(db, task, "added", due_date=None)

    assert result.tasks[0].due_date is None
    assert result.tasks[0].notes == "keep\nadded"


def test_an_explicit_null_blocked_by_still_clears_alongside_an_append(db):
    run_id = _run()
    other = _task(db, run_id, title="Blocker")
    task = _task(db, run_id, notes="keep", blocked_by=other.id)

    result = _append(db, task, "added", blocked_by=None)

    assert result.tasks[0].blocked_by is None
    assert result.tasks[0].notes == "keep\nadded"


# ------------------------------------------------ version, events, refusals


def test_one_append_is_one_version_and_one_event(db):
    run_id = _run()
    task = _task(db, run_id, notes="one")

    result = _append(db, task, "two")
    domain.write_events(run_id, ACTOR_ID, result.events, conn=db)
    db.commit()

    assert result.tasks[0].version == task.version + 1
    assert len(result.events) == 1

    rows = db.execute(
        "SELECT operation, before, after FROM task_events "
        " WHERE task_id = %(id)s ORDER BY id",
        {"id": task.id},
    ).fetchall()
    assert [row["operation"] for row in rows] == ["created", "updated"]

    updated = rows[-1]
    assert updated["before"]["notes"] == "one"
    assert updated["after"]["notes"] == "one\ntwo"
    assert updated["before"]["version"] == task.version
    assert updated["after"]["version"] == task.version + 1


def test_a_stale_expected_version_refuses_the_append(db):
    run_id = _run()
    task = _task(db, run_id, notes="one")

    domain.update_task(
        ACTOR_ID,
        UpdateTaskArgs(
            task_id=task.id, expected_version=task.version, notes="moved"
        ),
        conn=db,
    )
    db.commit()

    with pytest.raises(VersionConflictError):
        _append(db, task, "two")  # task still carries the old version


def test_a_missing_target_refuses_like_any_other_update(db):
    run_id = _run()
    _task(db, run_id)

    absent = domain.Task(
        id=uuid4(),
        owner_id=ACTOR_ID,
        title="gone",
        version=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )

    with pytest.raises(VersionConflictError):
        _append(db, absent, "two")


def test_another_actors_task_is_untouched_by_an_append(db):
    run_id = _run()
    foreign = _task(db, run_id, title="Theirs", actor_id=OTHER_ACTOR_ID, notes="theirs")

    with pytest.raises(VersionConflictError):
        _append(db, foreign, "mine")

    db.rollback()
    assert _notes(db, foreign.id) == "theirs"


# ----------------------------------------------------------------- replay


def test_a_completed_replay_does_not_append_twice(db):
    """The second call returns the stored result rather than merging again.

    This is the failure an append makes possible that a replacement does not:
    replaying a replacement writes the same value, while replaying an append
    would grow the notes every time.
    """
    run_id = _run()
    task = _task(db, run_id, notes="one")

    first = _append_through_the_tool(
        run_id, task, "two", tool_call_id="call-d78-0001"
    )
    second = _append_through_the_tool(
        run_id, task, "two", tool_call_id="call-d78-0001"
    )

    assert first == second
    assert first[0]["notes"] == "one\ntwo"
    assert _notes(db, task.id) == "one\ntwo"

    versions = db.execute(
        "SELECT version FROM tasks WHERE id = %(id)s", {"id": task.id}
    ).fetchone()
    assert versions["version"] == task.version + 1

    events = db.execute(
        "SELECT count(*) AS n FROM task_events "
        " WHERE task_id = %(id)s AND operation = 'updated'",
        {"id": task.id},
    ).fetchone()
    assert events["n"] == 1


def test_the_stored_lease_result_is_the_merged_value(db):
    run_id = _run()
    task = _task(db, run_id, notes="one")

    _append_through_the_tool(run_id, task, "two", tool_call_id="call-d78-0002")

    row = db.execute(
        "SELECT status, result FROM tool_invocations "
        " WHERE run_id = %(run_id)s AND tool_call_id = %(tool_call_id)s",
        {"run_id": run_id, "tool_call_id": "call-d78-0002"},
    ).fetchone()

    assert row["status"] == "completed"
    assert row["result"][0]["notes"] == "one\ntwo"
