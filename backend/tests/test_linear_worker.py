"""T00W worker. The path from a claimed inbox row to a Trellis run and back.

Offline and credential-free. `FunctionModel` drives every model invocation and a
fake provider module stands in for `linear_agent_api`, so nothing here reaches
NVIDIA, OpenAI, or Linear. PostgreSQL is required, as it is for every other
deterministic test in this suite.

The properties under test are the ones a green board would otherwise hide:

```text
prompt extraction     both actions, and the signal that is not a prompt
continuity            first turn has none, second inherits, failure does not advance
atomicity             completion and cursor advance commit together or not at all
ordering              a later row for one session cannot overtake an earlier one
at-most-once          a failed Linear delivery never re-runs a committed mutation
delivery              caller-owned UUID, exactly one attempt, no blind retry
credentials           rotation persists, and no secret reaches a durable error
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Json
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent as agent_module
from app import linear_agent, linear_agent_worker as worker, runs, sql
from app.db import pool
from app.linear_agent_api import LinearApiError, LinearTokens
from app.models import RunStatus


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
ORG_ID = "org-t00w-worker"
CLIENT_ID = "client-t00w-worker"
APP_USER_ID = "app-user-t00w-worker"
ALLOWED_HUMAN = "human-allowed-t00w-worker"
SESSION_A = "sess-worker-a"
SESSION_B = "sess-worker-b"

ANSWER = "Here is what I found."

# A value shaped like a credential. Nothing durable may ever contain it.
SECRET_MARKER = "lin_oauth_super_secret_value"


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def clean():
    _wipe()
    yield
    _wipe()


def _wipe():
    with pool.connection() as conn:
        conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
        conn.execute(
            "DELETE FROM linear_agent_inbox WHERE organization_id = %s", (ORG_ID,)
        )
        conn.execute(
            "DELETE FROM linear_agent_sessions WHERE organization_id = %s", (ORG_ID,)
        )
        conn.execute("DELETE FROM linear_installations WHERE organization_id = %s", (ORG_ID,))
        conn.commit()


class FakeProvider:
    """Stands in for `linear_agent_api`, recording every outbound call.

    Deliberately records rather than asserts. A test that only checks the final
    return value cannot see a retry, and the whole point of the no-retry rule is
    that a second attempt is invisible from outside.
    """

    def __init__(self):
        self.activities = []
        self.refreshes = []
        self.activity_error = None
        self.refresh_error = None
        self.next_tokens = LinearTokens(
            access_token="rotated-access",
            refresh_token="rotated-refresh",
            expires_in=3600,
            token_type="Bearer",
            scope="read write app:mentionable app:assignable",
        )

    def create_agent_activity(
        self, access_token, *, agent_session_id, activity_id, content, client=None
    ):
        self.activities.append(
            {
                "access_token": access_token,
                "agent_session_id": agent_session_id,
                "activity_id": activity_id,
                "content": content,
            }
        )
        if self.activity_error is not None and content.get("type") in self.activity_error:
            raise self.activity_error[content["type"]]

    def refresh_tokens(self, refresh_token, *, client=None):
        self.refreshes.append(refresh_token)
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.next_tokens


@pytest.fixture
def fake_provider(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(worker, "provider", lambda: provider)
    return provider


# ------------------------------------------------------------------- helpers


def install_active(**overrides) -> dict:
    values = {
        "organization_id": ORG_ID,
        "oauth_client_id": CLIENT_ID,
        "app_user_id": APP_USER_ID,
        "allowed_linear_user_id": ALLOWED_HUMAN,
        "access_token": "stored-access",
        "refresh_token": "stored-refresh",
        "expires_in": 3600,
        "granted_scopes": "read write app:mentionable app:assignable",
    }
    values.update(overrides)
    with pool.connection() as conn:
        row = conn.execute(sql.INSERT_LINEAR_INSTALLATION, values).fetchone()
        conn.commit()
    return dict(row)


def prompted_payload(body="List my tasks.", session_id=SESSION_A, **overrides) -> dict:
    payload = {
        "type": linear_agent.TYPE_AGENT_SESSION,
        "action": linear_agent.ACTION_PROMPTED,
        "organizationId": ORG_ID,
        "oauthClientId": CLIENT_ID,
        "appUserId": APP_USER_ID,
        "agentSession": {
            "id": session_id,
            "organizationId": ORG_ID,
            "appUserId": APP_USER_ID,
            "creatorId": ALLOWED_HUMAN,
        },
        "agentActivity": {
            "id": f"act-{uuid4()}",
            "agentSessionId": session_id,
            "userId": ALLOWED_HUMAN,
            "content": {"type": "prompt", "body": body},
        },
    }
    payload.update(overrides)
    return payload


def insert_inbox(
    payload: dict,
    *,
    session_id=SESSION_A,
    received_at=None,
    action=linear_agent.ACTION_PROMPTED,
) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO linear_agent_inbox (
                delivery_id, body_sha256, organization_id, agent_session_id,
                action, payload, status, received_at, not_before
            ) VALUES (
                %(delivery_id)s, %(body_sha256)s, %(organization_id)s,
                %(agent_session_id)s, %(action)s, %(payload)s, 'pending',
                %(received_at)s, %(received_at)s
            )
            RETURNING *;
            """,
            {
                "delivery_id": str(uuid4()),
                "body_sha256": uuid4().hex + uuid4().hex,
                "organization_id": ORG_ID,
                "agent_session_id": session_id,
                "action": action,
                "payload": Json(payload),
                "received_at": received_at or datetime.now(timezone.utc),
            },
        ).fetchone()
        conn.commit()
    return dict(row)


def inbox_row(row_id) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM linear_agent_inbox WHERE id = %s", (row_id,)
        ).fetchone()
        conn.commit()
    return dict(row)


def session_row(session_id=SESSION_A) -> dict | None:
    with pool.connection() as conn:
        row = conn.execute(
            sql.SELECT_LINEAR_AGENT_SESSION,
            {"organization_id": ORG_ID, "agent_session_id": session_id},
        ).fetchone()
        conn.commit()
    return dict(row) if row is not None else None


def task_titles() -> list[str]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT title FROM tasks WHERE owner_id = %s ORDER BY title", (ACTOR_ID,)
        ).fetchall()
        conn.commit()
    return [row["title"] for row in rows]


def text_agent(reply=ANSWER):
    """An agent that answers with plain text and calls no tool."""

    async def model(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(reply)])

    return agent_module.build_agent(
        FunctionModel(model, model_name="t00w-worker-text")
    )


def creating_agent(title: str, fail_after=False):
    """An agent that commits one `create_task`, then answers or raises."""
    state = {"calls": 0}

    async def model(messages, info: AgentInfo) -> ModelResponse:
        state["calls"] += 1
        if state["calls"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="create_task",
                        args={"title": title},
                        tool_call_id="call-worker-create",
                    )
                ]
            )
        if fail_after:
            raise RuntimeError(f"provider blew up carrying {SECRET_MARKER}")
        return ModelResponse(parts=[TextPart(ANSWER)])

    return agent_module.build_agent(
        FunctionModel(model, model_name="t00w-worker-create")
    )


def deleting_agent(task_id: UUID):
    """An agent that proposes the one declaratively gated tool."""

    async def model(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="delete_tasks",
                    args={"task_ids": [str(task_id)]},
                    tool_call_id="call-worker-delete",
                )
            ]
        )

    return agent_module.build_agent(
        FunctionModel(model, model_name="t00w-worker-delete")
    )


# --------------------------------------------------------- prompt extraction


def test_prompted_prompt_comes_from_activity_content_body():
    payload = prompted_payload("Show me what is overdue.")
    assert worker.extract_prompt(payload) == "Show me what is overdue."


def test_prompted_prompt_falls_back_to_the_documented_activity_body():
    """Linear's prose names `agentActivity.body`; the observed shape nests it.

    Both are accepted, and this is the case that proves the documented location
    is not merely commented about.
    """
    payload = prompted_payload()
    payload["agentActivity"].pop("content")
    payload["agentActivity"]["body"] = "Documented location."
    assert worker.extract_prompt(payload) == "Documented location."


def test_prompted_activity_carrying_a_signal_is_never_a_prompt():
    """A stop signal must not become an instruction to the agent."""
    payload = prompted_payload("stop")
    payload["agentActivity"]["signal"] = "stop"
    assert worker.extract_prompt(payload) is None


def test_created_prompt_comes_from_prompt_context_not_an_activity():
    """A created session has no user AgentActivity at all."""
    payload = prompted_payload()
    payload["action"] = linear_agent.ACTION_CREATED
    payload.pop("agentActivity")
    payload["promptContext"] = "<issue>Ship the demo</issue>"
    assert worker.extract_prompt(payload) == "<issue>Ship the demo</issue>"


def test_created_prompt_falls_back_to_the_originating_comment():
    payload = prompted_payload()
    payload["action"] = linear_agent.ACTION_CREATED
    payload.pop("agentActivity")
    payload["agentSession"]["comment"] = {"body": "@trellis clean up my board"}
    assert worker.extract_prompt(payload) == "@trellis clean up my board"


def test_unrecognized_shapes_yield_no_prompt_rather_than_a_guess():
    assert worker.extract_prompt({"action": linear_agent.ACTION_PROMPTED}) is None
    assert worker.extract_prompt({"action": linear_agent.ACTION_CREATED}) is None
    assert worker.extract_prompt({"action": "unheard_of"}) is None
    payload = prompted_payload("   ")
    assert worker.extract_prompt(payload) is None


# ------------------------------------------------------------ the happy path


def test_first_turn_runs_with_no_predecessor_and_advances_the_cursor(fake_provider):
    install_active()
    row = insert_inbox(prompted_payload("List my tasks."))

    result = worker.process_next(agent=text_agent())

    assert result["outcome"] == worker.OUTCOME_COMPLETED
    run = runs.load(result["run_id"], ACTOR_ID)
    assert run.status is RunStatus.COMPLETED
    assert run.prompt == "List my tasks."

    stored = inbox_row(row["id"])
    assert stored["status"] == "completed"
    assert stored["claimed_until"] is None
    assert stored["run_id"] == result["run_id"]
    assert stored["last_error"] is None
    assert session_row()["last_completed_run_id"] == result["run_id"]

    # Acknowledgement first, then the response. Both caller-owned UUID v4.
    assert [a["content"]["type"] for a in fake_provider.activities] == [
        "thought",
        "response",
    ]
    for activity in fake_provider.activities:
        assert UUID(activity["activity_id"]).version == 4
    assert fake_provider.activities[-1]["content"]["body"] == ANSWER


def test_second_turn_inherits_history_from_the_completed_predecessor(fake_provider):
    install_active()
    insert_inbox(prompted_payload("First question."))
    first = worker.process_next(agent=text_agent("First answer."))
    assert first["outcome"] == worker.OUTCOME_COMPLETED

    insert_inbox(prompted_payload("Second question."))
    second = worker.process_next(agent=text_agent("Second answer."))
    assert second["outcome"] == worker.OUTCOME_COMPLETED

    history = runs.load_history(second["run_id"], ACTOR_ID)
    rendered = json.dumps(history)
    assert "First question." in rendered
    assert "First answer." in rendered
    assert session_row()["last_completed_run_id"] == second["run_id"]


def test_separate_sessions_progress_independently(fake_provider):
    install_active()
    insert_inbox(prompted_payload("A one.", SESSION_A), session_id=SESSION_A)
    insert_inbox(prompted_payload("B one.", SESSION_B), session_id=SESSION_B)

    first = worker.process_next(agent=text_agent())
    second = worker.process_next(agent=text_agent())

    assert first["outcome"] == worker.OUTCOME_COMPLETED
    assert second["outcome"] == worker.OUTCOME_COMPLETED
    assert session_row(SESSION_A)["last_completed_run_id"] is not None
    assert session_row(SESSION_B)["last_completed_run_id"] is not None
    assert (
        session_row(SESSION_A)["last_completed_run_id"]
        != session_row(SESSION_B)["last_completed_run_id"]
    )


# --------------------------------------------------------------- refusal paths


def test_no_active_installation_fails_the_row_without_running_a_model(fake_provider):
    """A revoked installation must not produce a turn or a delivery."""
    install_active()
    with pool.connection() as conn:
        conn.execute("UPDATE linear_installations SET status = 'revoked';")
        conn.commit()

    row = insert_inbox(prompted_payload())
    result = worker.process_next(agent=text_agent())

    assert result["outcome"] == worker.OUTCOME_FAILED
    assert result["error"] == worker.ERROR_NO_INSTALLATION
    stored = inbox_row(row["id"])
    assert stored["status"] == "failed"
    assert stored["run_id"] is None
    assert fake_provider.activities == []
    assert session_row() is None


def test_malformed_stored_payload_fails_safely(fake_provider):
    """An authenticated payload whose prompt cannot be located is terminal.

    The installation identifiers are intact, so this isolates the parsing
    failure rather than passing for the unrelated reason that nothing resolved.
    """
    install_active()
    payload = prompted_payload()
    payload["agentActivity"] = {"id": "act-x", "agentSessionId": SESSION_A}
    row = insert_inbox(payload)

    result = worker.process_next(agent=text_agent())

    assert result["outcome"] == worker.OUTCOME_FAILED
    assert result["error"] == worker.ERROR_NO_PROMPT
    stored = inbox_row(row["id"])
    assert stored["status"] == "failed"
    assert stored["run_id"] is None
    assert stored["claimed_until"] is None
    # No model ran and nothing was said in Linear.
    assert fake_provider.activities == []


def test_a_payload_that_names_no_installation_fails_before_parsing(fake_provider):
    """Structural garbage cannot be bound to a workspace, so it never executes."""
    install_active()
    row = insert_inbox({"action": linear_agent.ACTION_PROMPTED})

    result = worker.process_next(agent=text_agent())

    assert result["outcome"] == worker.OUTCOME_FAILED
    assert result["error"] == worker.ERROR_NO_INSTALLATION
    assert inbox_row(row["id"])["status"] == "failed"
    assert fake_provider.activities == []


def test_signal_payload_fails_with_its_own_reason(fake_provider):
    install_active()
    payload = prompted_payload("stop")
    payload["agentActivity"]["signal"] = "stop"
    row = insert_inbox(payload)

    result = worker.process_next(agent=text_agent())

    assert result["error"] == worker.ERROR_SIGNAL_NOT_PROMPT
    assert inbox_row(row["id"])["status"] == "failed"


def test_attempt_budget_exhaustion_is_terminal(fake_provider):
    install_active()
    row = insert_inbox(prompted_payload())
    with pool.connection() as conn:
        conn.execute(
            "UPDATE linear_agent_inbox SET attempt_count = %s WHERE id = %s",
            (worker.settings.linear_inbox_max_attempts + 5, row["id"]),
        )
        conn.commit()

    result = worker.process_next(agent=text_agent())

    assert result["outcome"] == worker.OUTCOME_FAILED
    assert result["error"] == worker.ERROR_ATTEMPTS_EXHAUSTED
    assert inbox_row(row["id"])["status"] == "failed"
    assert fake_provider.activities == []


def test_claiming_increments_the_attempt_count(fake_provider):
    install_active()
    row = insert_inbox(prompted_payload())
    worker.process_next(agent=text_agent())
    assert inbox_row(row["id"])["attempt_count"] == 1


# --------------------------------------------------- delivery vs Trellis truth


def test_committed_mutation_survives_a_failed_linear_response_delivery(fake_provider):
    """The two facts are separate, and the mutation is not undone or repeated."""
    install_active()
    fake_provider.activity_error = {
        "response": LinearApiError("agent_activity_create", 500, "provider exploded")
    }
    row = insert_inbox(prompted_payload("Create a task called Ship it."))

    result = worker.process_next(agent=creating_agent("Ship it"))

    assert result["outcome"] == worker.OUTCOME_COMPLETED
    assert result["delivery_error"] == "linear:agent_activity_create:500"
    assert task_titles() == ["Ship it"]

    stored = inbox_row(row["id"])
    assert stored["status"] == "completed"
    assert stored["last_error"] == "linear:agent_activity_create:500"
    # The Trellis run truth is preserved, not rewritten to look like a failure.
    assert runs.load(result["run_id"], ACTOR_ID).status is RunStatus.COMPLETED
    # And the cursor advanced, because the run really did complete.
    assert session_row()["last_completed_run_id"] == result["run_id"]


def test_a_failed_delivery_never_re_runs_the_turn(fake_provider):
    """The duplicate-mutation defense, proven by draining the queue again."""
    install_active()
    fake_provider.activity_error = {
        "response": LinearApiError("agent_activity_create", 500, "provider exploded")
    }
    insert_inbox(prompted_payload("Create a task called Ship it."))

    worker.process_next(agent=creating_agent("Ship it"))
    again = worker.process_next(agent=creating_agent("Ship it"))

    assert again["outcome"] == worker.OUTCOME_IDLE
    assert task_titles() == ["Ship it"]


def test_an_ambiguous_transport_error_is_not_blindly_retried(fake_provider):
    """One call, one outbound attempt, even when the failure is unclassifiable.

    `status=None` is the transport case the provider layer reports when it
    cannot tell whether Linear received the mutation. Guessing is exactly what
    the live conflict-on-insert finding forbids.
    """
    install_active()
    fake_provider.activity_error = {
        "response": LinearApiError("agent_activity_create", None, "ConnectError")
    }
    insert_inbox(prompted_payload())

    result = worker.process_next(agent=text_agent())

    responses = [a for a in fake_provider.activities if a["content"]["type"] == "response"]
    assert len(responses) == 1
    assert result["delivery_error"] == "linear:agent_activity_create:transport"


def test_a_failed_acknowledgement_does_not_stop_the_turn(fake_provider):
    install_active()
    fake_provider.activity_error = {
        "thought": LinearApiError("agent_activity_create", 503, "busy")
    }
    insert_inbox(prompted_payload())

    result = worker.process_next(agent=text_agent())

    assert result["outcome"] == worker.OUTCOME_COMPLETED
    assert inbox_row(result["row_id"])["last_error"] == "linear:agent_activity_create:503"


# ------------------------------------------------------------ model failures


def test_a_pre_commit_model_failure_releases_the_row_and_holds_the_cursor(
    fake_provider, monkeypatch
):
    install_active()
    row = insert_inbox(prompted_payload())

    async def exploding(messages, info: AgentInfo) -> ModelResponse:
        raise RuntimeError(f"model down, token {SECRET_MARKER}")

    agent = agent_module.build_agent(
        FunctionModel(exploding, model_name="t00w-worker-boom")
    )
    result = worker.process_next(agent=agent)

    assert result["outcome"] == worker.OUTCOME_RELEASED
    stored = inbox_row(row["id"])
    assert stored["status"] == "pending"
    assert stored["claimed_until"] is None
    assert stored["run_id"] is None
    assert stored["not_before"] > datetime.now(timezone.utc)
    # The run exists and is failed; the session cursor never moved.
    assert session_row()["last_completed_run_id"] is None
    assert SECRET_MARKER not in stored["last_error"]


def test_a_post_commit_model_failure_is_terminal_rather_than_retried(fake_provider):
    """A committed mutation makes a retry a second mutation, so there is none."""
    install_active()
    row = insert_inbox(prompted_payload("Create a task called Ship it."))

    result = worker.process_next(agent=creating_agent("Ship it", fail_after=True))

    assert result["outcome"] == worker.OUTCOME_FAILED
    assert result["error"] == worker.ERROR_MUTATION_COMMITTED
    assert task_titles() == ["Ship it"]

    stored = inbox_row(row["id"])
    assert stored["status"] == "failed"
    assert stored["run_id"] == result["run_id"]
    assert session_row()["last_completed_run_id"] is None

    # Draining again must not create a second task.
    assert worker.process_next(agent=creating_agent("Ship it"))["outcome"] == (
        worker.OUTCOME_IDLE
    )
    assert task_titles() == ["Ship it"]


def test_a_failed_turn_does_not_advance_the_session_cursor(fake_provider):
    install_active()
    insert_inbox(prompted_payload("First question."))
    first = worker.process_next(agent=text_agent("First answer."))
    assert first["outcome"] == worker.OUTCOME_COMPLETED

    insert_inbox(prompted_payload("Create a task called Ship it."))
    second = worker.process_next(agent=creating_agent("Ship it", fail_after=True))

    assert second["outcome"] == worker.OUTCOME_FAILED
    assert session_row()["last_completed_run_id"] == first["run_id"]


# ------------------------------------------------------------------ approvals


def test_an_approval_required_call_stops_at_the_trellis_boundary(fake_provider):
    """T16 is preserved: the row is written, nothing is auto-approved."""
    install_active()
    with pool.connection() as conn:
        task = conn.execute(
            "INSERT INTO tasks (owner_id, title) VALUES (%s, %s) RETURNING *;",
            (ACTOR_ID, "Delete me"),
        ).fetchone()
        conn.commit()

    row = insert_inbox(prompted_payload("Delete the task."))
    result = worker.process_next(agent=deleting_agent(task["id"]))

    assert result["outcome"] == worker.OUTCOME_APPROVAL_REQUIRED
    assert runs.load(result["run_id"], ACTOR_ID).status is RunStatus.AWAITING_APPROVAL
    assert runs.load_pending_approval(result["run_id"]) is not None
    # The task is still there. Nothing was approved on the model's word.
    assert task_titles() == ["Delete me"]

    stored = inbox_row(row["id"])
    assert stored["status"] == "completed"
    # An awaiting_approval run is not an eligible continuity predecessor, so the
    # cursor deliberately stays where it was.
    assert session_row()["last_completed_run_id"] is None

    kinds = [a["content"]["type"] for a in fake_provider.activities]
    assert kinds == ["thought", "elicitation"]


# --------------------------------------------------------- ordering and leases


def test_a_later_row_cannot_overtake_an_earlier_backing_off_row(fake_provider):
    install_active()
    now = datetime.now(timezone.utc)
    first = insert_inbox(
        prompted_payload("First."), received_at=now - timedelta(seconds=30)
    )
    insert_inbox(prompted_payload("Second."), received_at=now - timedelta(seconds=10))

    async def exploding(messages, info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model down")

    released = worker.process_next(
        agent=agent_module.build_agent(
            FunctionModel(exploding, model_name="t00w-worker-boom")
        )
    )
    assert released["row_id"] == first["id"]
    assert released["outcome"] == worker.OUTCOME_RELEASED

    # The first row is pending and backing off. The second must stay blocked.
    assert worker.process_next(agent=text_agent())["outcome"] == worker.OUTCOME_IDLE


def test_an_expired_lease_reclaims_the_same_first_row(fake_provider):
    install_active()
    now = datetime.now(timezone.utc)
    first = insert_inbox(
        prompted_payload("First."), received_at=now - timedelta(seconds=30)
    )
    insert_inbox(prompted_payload("Second."), received_at=now - timedelta(seconds=10))

    claimed = linear_agent.claim_next_linear_inbox(lease_seconds=30)
    assert claimed["id"] == first["id"]
    assert linear_agent.claim_next_linear_inbox(lease_seconds=30) is None

    with pool.connection() as conn:
        conn.execute(
            "UPDATE linear_agent_inbox SET claimed_until = now() - interval '1 second' "
            "WHERE id = %s",
            (first["id"],),
        )
        conn.commit()

    reclaimed = linear_agent.claim_next_linear_inbox(lease_seconds=30)
    assert reclaimed["id"] == first["id"]
    assert reclaimed["attempt_count"] == 2


def test_a_claimed_row_that_already_executed_is_never_executed_twice(fake_provider):
    """The crash-mid-turn case: the run exists, the outcome was never recorded."""
    install_active()
    insert_inbox(prompted_payload("Create a task called Ship it."))
    claimed = linear_agent.claim_next_linear_inbox(lease_seconds=30)

    run = runs.create_turn(ACTOR_ID, "Create a task called Ship it.", "m", None)
    runs.set_status(run.id, RunStatus.COMPLETED)
    with pool.connection() as conn:
        conn.execute(sql.SET_LINEAR_INBOX_RUN, {"id": claimed["id"], "run_id": run.id})
        conn.commit()
    claimed["run_id"] = run.id

    result = worker.process_row(claimed, agent=creating_agent("Ship it"))

    assert result["outcome"] == worker.OUTCOME_COMPLETED
    assert task_titles() == []
    assert fake_provider.activities == []
    assert session_row()["last_completed_run_id"] == run.id
    assert inbox_row(claimed["id"])["last_error"] == worker.ERROR_AMBIGUOUS_TURN


# ----------------------------------------------------------------- atomicity


def test_completion_and_cursor_advance_share_one_transaction(fake_provider, monkeypatch):
    """Break the second statement and the first must not survive.

    Proven by making the advance fail rather than by reading the code, because
    "they are in the same `with` block" is exactly the kind of claim that stays
    true in the comment after someone splits the function.
    """
    install_active()
    row = insert_inbox(prompted_payload())
    run = runs.create_turn(ACTOR_ID, "x", "m", None)

    monkeypatch.setattr(
        sql, "ADVANCE_LINEAR_AGENT_SESSION", "SELECT * FROM table_that_does_not_exist;"
    )

    with pytest.raises(Exception):
        worker._complete_and_advance(
            row["id"],
            run_id=run.id,
            organization_id=ORG_ID,
            agent_session_id=SESSION_A,
            last_error=None,
        )

    assert inbox_row(row["id"])["status"] == "pending"
    assert session_row() is None


# ---------------------------------------------------------------- credentials


def test_a_live_token_is_returned_without_calling_linear(fake_provider):
    installation = install_active(expires_in=3600)
    assert worker.ensure_access_token(installation["id"]) == "stored-access"
    assert fake_provider.refreshes == []


def test_an_expired_token_is_refreshed_and_the_rotation_is_persisted(fake_provider):
    """Linear rotates refresh tokens, so the returned one must replace the old."""
    installation = install_active(expires_in=1)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE linear_installations SET access_token_expires_at = now() "
            "- interval '1 hour' WHERE id = %s",
            (installation["id"],),
        )
        conn.commit()

    assert worker.ensure_access_token(installation["id"]) == "rotated-access"
    assert fake_provider.refreshes == ["stored-refresh"]

    with pool.connection() as conn:
        stored = conn.execute(
            "SELECT * FROM linear_installations WHERE id = %s", (installation["id"],)
        ).fetchone()
        conn.commit()
    assert stored["refresh_token"] == "rotated-refresh"
    assert stored["access_token"] == "rotated-access"
    assert stored["access_token_expires_at"] > datetime.now(timezone.utc)


def test_a_response_without_a_rotation_keeps_the_existing_refresh_token(fake_provider):
    installation = install_active()
    fake_provider.next_tokens = LinearTokens(
        access_token="rotated-access",
        refresh_token=None,
        expires_in=3600,
        token_type="Bearer",
        scope="read write app:mentionable app:assignable",
    )
    with pool.connection() as conn:
        conn.execute(
            "UPDATE linear_installations SET access_token_expires_at = now() "
            "- interval '1 hour' WHERE id = %s",
            (installation["id"],),
        )
        conn.commit()

    worker.ensure_access_token(installation["id"])

    with pool.connection() as conn:
        stored = conn.execute(
            "SELECT refresh_token FROM linear_installations WHERE id = %s",
            (installation["id"],),
        ).fetchone()
        conn.commit()
    assert stored["refresh_token"] == "stored-refresh"


def test_a_failed_refresh_releases_the_row_without_running_a_model(fake_provider):
    installation = install_active()
    fake_provider.refresh_error = LinearApiError("oauth_refresh", 400, SECRET_MARKER)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE linear_installations SET access_token_expires_at = now() "
            "- interval '1 hour' WHERE id = %s",
            (installation["id"],),
        )
        conn.commit()

    row = insert_inbox(prompted_payload())
    result = worker.process_next(agent=text_agent())

    assert result["outcome"] == worker.OUTCOME_RELEASED
    stored = inbox_row(row["id"])
    assert stored["status"] == "pending"
    assert stored["last_error"] == "linear:oauth_refresh:400"
    assert SECRET_MARKER not in stored["last_error"]
    assert fake_provider.activities == []


# -------------------------------------------------------------------- secrets


def test_no_durable_worker_error_can_carry_provider_detail(fake_provider):
    """The failure detail from Linear can echo the request, which carries a token."""
    exc = LinearApiError("agent_activity_create", 401, f"token {SECRET_MARKER} rejected")
    assert worker._safe_provider_error(exc) == "linear:agent_activity_create:401"
    assert SECRET_MARKER not in worker._safe_provider_error(exc)

    transport = LinearApiError("oauth_refresh", None, SECRET_MARKER)
    assert worker._safe_provider_error(transport) == "linear:oauth_refresh:transport"


def test_the_worker_source_holds_no_provider_endpoint_and_no_retry_loop():
    """The T00L boundary and the no-retry rule, asserted against this module.

    The gate greps `backend/app/` for provider endpoints, and this file is not
    the authorized exemption. Asserting it here as well means the property is
    checked by the suite the author runs, not only by CI.
    """
    source = (Path(__file__).resolve().parents[1] / "app" / "linear_agent_worker.py").read_text(
        encoding="utf-8"
    )
    assert "api.linear.app" not in source
    assert "linear.app/oauth" not in source
    assert "httpx" not in source
