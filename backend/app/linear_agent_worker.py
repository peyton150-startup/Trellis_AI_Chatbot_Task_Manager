"""T00W worker: one claimed inbox row becomes one Trellis application run. D-69.

`linear_agent.py` owns the untrusted ingress and stops at a committed row.
This module owns everything after that, and the split is the same one the rest
of the build uses: the webhook must answer Linear inside five seconds, so a
model invocation and an outbound provider round trip cannot live on that path.

```text
claim                    lease one row, per-session serialized, in SQL
installation             the authoritative ACTIVE row, or terminal failure
prompt                   derived from the stored authenticated payload
continuity               last_completed_run_id, server-owned
run                      runs.create_turn, a fresh application run
history                  runs.load_history, the single source
model                    agent.get_agent().run, invoked directly
persist                  history, usage, terminal status
deliver                  one AgentActivity, caller-owned UUID
finalize                 completion and cursor advance, one transaction
```

**The turn executes at most once per inbox row, and that is enforced by state
rather than by care.** `inbox.run_id` is written before the model is invoked. A
claimed row that already carries a run id is a turn whose outcome this process
cannot re-derive, so it is finalized from the run's own status and never
executed again. The one path that clears the id back to NULL is the transient
release, and it is reachable only after `RunEffects.mutation_committed` proves
no tool committed.

**Trellis mutation success and Linear delivery success are separate facts.** A
committed `delete_tasks` followed by a failed `agentActivityCreate` is a
completed Trellis run and a failed delivery, in that order, and the worker
records exactly that. It does not fail the run, does not compensate, and does
not re-invoke the model, because re-invoking it is how one deletion becomes two.
The delivery failure lands in `last_error` on a row that is already `completed`.

**No retry is added around AgentActivity.** Live T00W probe: resubmitting the
same activity UUID returned HTTP 200 carrying the GraphQL error "conflict on
insert of AgentActivity". The caller-owned id is therefore a duplicate detector
at the provider and not a replay token, and `linear_agent_api._request` issues
exactly one attempt. Nothing here wraps that in a loop.

**Prompt extraction is deliberately one small pure function.** Linear's prose
documentation and the payload shape observed on the wire describe the prompted
message differently, and the candidates in `extract_prompt` are ordered
possibilities rather than a settled contract. It has no side effects and no
database access precisely so a live payload can correct it without touching
anything that executes, commits, or delivers. An unrecognized shape is a
terminal, explicit failure; it is never guessed at, and a non-null `signal` is
never treated as prompt text.

**Secrets never reach an error string.** Every durable error is built from a
fixed vocabulary plus, for provider failures, an operation name and a status
code. Access tokens, refresh tokens, client secrets, webhook secrets, and stored
payloads are never interpolated into `last_error`, an exception message, or a
log line.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelMessagesTypeAdapter

from . import agent as agent_module
from . import linear_agent, runs, sql
from .config import settings
from .db import pool
from .linear_agent_api import LinearApiError
from .models import RunStatus


# Durable, machine-readable worker outcomes. Never prose and never a payload.
OUTCOME_IDLE = "idle"
OUTCOME_COMPLETED = "completed"
OUTCOME_APPROVAL_REQUIRED = "approval_required"
OUTCOME_FAILED = "failed"
OUTCOME_RELEASED = "released"

# Durable error vocabulary. Each value is a fixed string; the only variable
# parts anywhere are a provider operation name and an HTTP status, both of which
# are safe to store. See the module docstring.
ERROR_NO_INSTALLATION = "no_active_installation"
ERROR_INSTALLATION_MISMATCH = "installation_does_not_match_event"
ERROR_UNUSABLE_PAYLOAD = "unusable_stored_payload"
ERROR_NO_PROMPT = "no_extractable_prompt"
ERROR_SIGNAL_NOT_PROMPT = "activity_carried_a_signal_not_a_prompt"
ERROR_ATTEMPTS_EXHAUSTED = "attempt_budget_exhausted"
ERROR_AMBIGUOUS_TURN = "turn_already_executed_outcome_ambiguous"
ERROR_APPROVAL_REQUIRED = "approval_required_decide_in_trellis"
ERROR_MUTATION_COMMITTED = "mutation_committed_response_incomplete"
ERROR_MODEL_FAILED = "model_invocation_failed"

# What the human sees in Linear. Fixed text, because these are the worker's own
# words and never an echo of a payload or of an exception.
ACK_BODY = "Working on it. Checking your Trellis tasks."
APPROVAL_BODY = (
    "This request needs approval before it can run. Trellis has opened an "
    "approval card; approve or reject it in the Trellis app and I will "
    "continue there. I will not act on it from Linear."
)
FAILURE_BODY = "I could not complete that request. Nothing was changed."
PARTIAL_BODY = (
    "Some task changes were committed, but I could not finish the response. "
    "The Trellis board is authoritative; please check it."
)


class _Terminal(Exception):
    """A permanent failure for this row. A retry reaches the same answer."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _Transient(Exception):
    """A local failure a later attempt may resolve, with nothing committed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# ------------------------------------------------------------ prompt parsing


def _text(value: Any) -> str | None:
    """A non-empty string, or None. Whitespace alone is not a prompt."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def extract_prompt(payload: dict) -> str | None:
    """The accepted user prompt, from the stored authenticated payload only.

    Pure, so the shapes below can be corrected against a live payload without
    touching anything that executes, commits, or delivers.

    The two actions do not carry the prompt in the same place and are not
    conflated. `prompted` carries a user-authored AgentActivity. `created`
    carries the context of the mention or delegation that opened the session and
    has no activity at all, which is why reusing the prompted extractor for it
    would silently produce an empty turn.

    **Ordered candidates, not a settled contract.** Linear's documentation names
    the prompted body as `agentActivity.body`, while the payload observed on the
    wire nests content under `agentActivity.content`. Both are accepted, content
    first. An unrecognized shape returns None and the caller fails the row
    explicitly rather than inventing text for the model to act on.

    A non-null `signal` returns None regardless of any body present. A signal is
    a control message such as a stop request, and running it as a prompt would
    turn "stop" into an instruction to the agent.
    """
    action = payload.get("action")

    if action == linear_agent.ACTION_PROMPTED:
        activity = payload.get("agentActivity")
        if not isinstance(activity, dict):
            return None
        if activity.get("signal") is not None:
            return None

        content = activity.get("content")
        if isinstance(content, dict):
            body = _text(content.get("body"))
            if body is not None:
                return body
        elif isinstance(content, str):
            body = _text(content)
            if body is not None:
                return body
        return _text(activity.get("body"))

    if action == linear_agent.ACTION_CREATED:
        # `promptContext` is the formatted string Linear assembles from the
        # mention, the issue, and the surrounding comments. It is the documented
        # location for a created session and the only field here that describes
        # the whole request rather than one fragment of it.
        context = _text(payload.get("promptContext"))
        if context is not None:
            return context

        session = payload.get("agentSession")
        if isinstance(session, dict):
            comment = session.get("comment")
            if isinstance(comment, dict):
                body = _text(comment.get("body"))
                if body is not None:
                    return body
            issue = session.get("issue")
            if isinstance(issue, dict):
                title = _text(issue.get("title"))
                if title is not None:
                    return title
        return None

    return None


# --------------------------------------------------------------- credentials


def provider():
    """The provider boundary, resolved at call time.

    A function rather than a module-level binding, so a deterministic test can
    substitute the boundary without reaching into `sys.modules`, and so the
    single point of Linear egress stays visible rather than implied.
    """
    from . import linear_agent_api

    return linear_agent_api


def _safe_provider_error(exc: LinearApiError) -> str:
    """A durable error built from an operation and a status. Never a body."""
    status = exc.status if exc.status is not None else "transport"
    return f"linear:{exc.operation}:{status}"


def ensure_access_token(installation_id: UUID) -> str:
    """The live access token for one installation, refreshing under a lock.

    The lock is the mechanism, not a precaution. Linear rotates refresh tokens,
    so two workers that both observed an expired token and both refreshed would
    leave one of them persisting a value the provider has already superseded,
    and the stored credential would be dead until the next install. `FOR UPDATE`
    makes the expiry test and the replacement inseparable: the second worker
    waits, re-reads inside the same lock, sees a live token, and returns it
    without calling Linear at all.

    This deliberately holds a transaction across a provider call, which is the
    opposite of what `linear_install.py` does. The two protect different things.
    The callback's rule is about a single-use state value, where holding a
    transaction buys nothing. This is mutual exclusion over a rotating
    credential, where the lock is the entire point. The provider timeout bounds
    how long it can be held.
    """
    with pool.connection() as conn:
        row = conn.execute(sql.LOCK_ACTIVE_LINEAR_INSTALLATION).fetchone()
        if row is None or row["id"] != installation_id:
            conn.commit()
            raise _Terminal(ERROR_NO_INSTALLATION)

        now = conn.execute("SELECT now() AS now;").fetchone()["now"]
        expires_at = row["access_token_expires_at"]
        # A minute of headroom, so a token that would expire mid-request is
        # refreshed before it is used rather than after it fails.
        if expires_at is not None and (expires_at - now).total_seconds() > 60:
            token = row["access_token"]
            conn.commit()
            return token

        refresh_token = row["refresh_token"]
        if not refresh_token:
            conn.commit()
            raise _Terminal(ERROR_NO_INSTALLATION)

        try:
            tokens = provider().refresh_tokens(refresh_token)
        except LinearApiError as exc:
            conn.rollback()
            raise _Transient(_safe_provider_error(exc)) from None

        updated = conn.execute(
            sql.UPDATE_LINEAR_INSTALLATION_TOKENS,
            {
                "id": installation_id,
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_in": tokens.expires_in,
            },
        ).fetchone()
        conn.commit()
    return updated["access_token"]


# ------------------------------------------------------------------ delivery


def _emit(access_token: str, session_id: str, content: dict) -> str | None:
    """Post one Agent Activity with a caller-owned UUID. Never retried.

    Returns a durable error string on failure rather than raising, because every
    caller's answer to a delivery failure is the same: record it and continue
    with whatever Trellis truth already exists. See the module docstring on the
    live conflict-on-insert finding.
    """
    try:
        provider().create_agent_activity(
            access_token,
            agent_session_id=session_id,
            activity_id=str(uuid4()),
            content=content,
        )
    except LinearApiError as exc:
        return _safe_provider_error(exc)
    return None


# -------------------------------------------------------------- finalization


def _complete_and_advance(
    row_id: UUID,
    *,
    run_id: UUID,
    organization_id: str,
    agent_session_id: str,
    last_error: str | None,
) -> None:
    """Mark the turn completed and advance the session cursor, atomically.

    One connection, two statements, one commit. Splitting them would allow a
    crash to leave a completed turn whose history the next turn cannot inherit,
    or a cursor naming a run whose inbox row is still claimable.
    """
    with pool.connection() as conn:
        conn.execute(
            sql.COMPLETE_LINEAR_INBOX,
            {"id": row_id, "run_id": run_id, "last_error": last_error},
        )
        conn.execute(
            sql.ADVANCE_LINEAR_AGENT_SESSION,
            {
                "organization_id": organization_id,
                "agent_session_id": agent_session_id,
                "run_id": run_id,
            },
        )
        conn.commit()


def _complete_without_advancing(
    row_id: UUID, *, run_id: UUID | None, last_error: str | None
) -> None:
    """The row is handled and must never run again, but is no continuity source.

    Used when a turn stopped at an approval boundary. The run is
    `awaiting_approval`, which `runs.create_turn` refuses as a predecessor, so
    advancing the cursor to it would break the next turn rather than help it.
    """
    with pool.connection() as conn:
        conn.execute(
            sql.COMPLETE_LINEAR_INBOX,
            {"id": row_id, "run_id": run_id, "last_error": last_error},
        )
        conn.commit()


def _fail(row_id: UUID, *, run_id: UUID | None, last_error: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            sql.FAIL_LINEAR_INBOX,
            {"id": row_id, "run_id": run_id, "last_error": last_error},
        )
        conn.commit()


def _release(row_id: UUID, *, backoff_seconds: int, last_error: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            sql.RELEASE_LINEAR_INBOX,
            {
                "id": row_id,
                "backoff_seconds": backoff_seconds,
                "last_error": last_error,
            },
        )
        conn.commit()


def _backoff_seconds(attempt_count: int) -> int:
    """Bounded exponential backoff, from the attempts the row has already spent."""
    return min(600, 5 * (2 ** max(0, attempt_count - 1)))


# ---------------------------------------------------------------- the worker


def process_next(*, agent=None, lease_seconds: int | None = None) -> dict:
    """Claim one row and take it to a terminal or safely retryable state.

    Returns a machine-readable outcome and never raises for an ordinary
    failure, because a worker loop that dies on one bad row stops draining the
    whole queue.
    """
    lease = lease_seconds or settings.linear_inbox_lease_seconds
    row = linear_agent.claim_next_linear_inbox(lease_seconds=lease)
    if row is None:
        return {"outcome": OUTCOME_IDLE, "row_id": None}
    return process_row(row, agent=agent)


def process_row(row: dict, *, agent=None) -> dict:
    """Everything that happens to one already-claimed row."""
    row_id = row["id"]

    # The duplicate-execution guard, before anything else. A claimed row that
    # already names a run is a turn whose outcome this process cannot
    # re-derive, so it is finalized from the run and never executed again.
    if row["run_id"] is not None:
        return _finalize_ambiguous(row)

    if row["attempt_count"] > settings.linear_inbox_max_attempts:
        _fail(row_id, run_id=None, last_error=ERROR_ATTEMPTS_EXHAUSTED)
        return {
            "outcome": OUTCOME_FAILED,
            "row_id": row_id,
            "error": ERROR_ATTEMPTS_EXHAUSTED,
        }

    try:
        return _execute(row, agent=agent)
    except _Terminal as exc:
        _fail(row_id, run_id=None, last_error=exc.reason)
        return {"outcome": OUTCOME_FAILED, "row_id": row_id, "error": exc.reason}
    except _Transient as exc:
        _release(
            row_id,
            backoff_seconds=_backoff_seconds(row["attempt_count"]),
            last_error=exc.reason,
        )
        return {"outcome": OUTCOME_RELEASED, "row_id": row_id, "error": exc.reason}


def _finalize_ambiguous(row: dict) -> dict:
    """A claimed row that already executed. Finalize it, never re-execute it.

    The run's own status is the truth. A completed run advances the cursor even
    though this process never saw it finish, because the run record is the
    durable fact and the inbox row is bookkeeping about it.
    """
    row_id = row["id"]
    run_id = row["run_id"]
    run = runs.load(run_id, settings.actor_id)

    if run.status is RunStatus.COMPLETED:
        _complete_and_advance(
            row_id,
            run_id=run_id,
            organization_id=row["organization_id"],
            agent_session_id=row["agent_session_id"],
            last_error=ERROR_AMBIGUOUS_TURN,
        )
        return {"outcome": OUTCOME_COMPLETED, "row_id": row_id, "run_id": run_id}

    if run.status is RunStatus.AWAITING_APPROVAL:
        _complete_without_advancing(
            row_id, run_id=run_id, last_error=ERROR_APPROVAL_REQUIRED
        )
        return {
            "outcome": OUTCOME_APPROVAL_REQUIRED,
            "row_id": row_id,
            "run_id": run_id,
        }

    _fail(row_id, run_id=run_id, last_error=ERROR_AMBIGUOUS_TURN)
    return {"outcome": OUTCOME_FAILED, "row_id": row_id, "error": ERROR_AMBIGUOUS_TURN}


def _load_installation_for(row: dict) -> dict:
    """The authoritative ACTIVE installation this row's event belongs to.

    Re-resolved here rather than trusted from ingress. An installation can be
    revoked between acceptance and execution, and a revoked installation must
    not produce a turn or a delivery.
    """
    payload = row["payload"]
    if not isinstance(payload, dict):
        raise _Terminal(ERROR_UNUSABLE_PAYLOAD)

    with pool.connection() as conn:
        installation = conn.execute(
            sql.SELECT_LINEAR_INSTALLATION_FOR_EVENT,
            {
                "organization_id": payload.get("organizationId"),
                "oauth_client_id": payload.get("oauthClientId"),
                "app_user_id": payload.get("appUserId"),
            },
        ).fetchone()
        conn.commit()

    if installation is None:
        raise _Terminal(ERROR_NO_INSTALLATION)
    if installation["organization_id"] != row["organization_id"]:
        raise _Terminal(ERROR_INSTALLATION_MISMATCH)
    return dict(installation)


def _continuity_run_id(organization_id: str, agent_session_id: str) -> UUID | None:
    """The server-owned predecessor for this session, or None for a first turn."""
    with pool.connection() as conn:
        row = conn.execute(
            sql.UPSERT_LINEAR_AGENT_SESSION,
            {
                "organization_id": organization_id,
                "agent_session_id": agent_session_id,
            },
        ).fetchone()
        conn.commit()
    return row["last_completed_run_id"]


def _execute(row: dict, *, agent=None) -> dict:
    """The turn itself.

    `_Terminal` and `_Transient` are raised from this function only before the
    model is invoked. After that point the outcome is finalized here, because a
    retry past that line is a duplicate-mutation risk rather than a second
    chance.
    """
    row_id = row["id"]
    payload = row["payload"]
    session_id = row["agent_session_id"]

    installation = _load_installation_for(row)

    prompt = extract_prompt(payload)
    if prompt is None:
        activity = payload.get("agentActivity")
        raise _Terminal(
            ERROR_SIGNAL_NOT_PROMPT
            if isinstance(activity, dict) and activity.get("signal") is not None
            else ERROR_NO_PROMPT
        )

    access_token = ensure_access_token(installation["id"])

    # Acknowledge before the model runs. Linear asks for a thought within ten
    # seconds and a model turn can exceed that, so this is the difference
    # between a visible agent and a silent "Working...". A failed
    # acknowledgement is recorded and does not stop the turn: it is courtesy,
    # not correctness.
    ack_error = _emit(access_token, session_id, {"type": "thought", "body": ACK_BODY})

    continuity_run_id = _continuity_run_id(row["organization_id"], session_id)
    run = runs.create_turn(
        settings.actor_id, prompt, settings.model_id, continuity_run_id
    )

    # Before the model. See the module docstring: this write is the guard.
    with pool.connection() as conn:
        conn.execute(sql.SET_LINEAR_INBOX_RUN, {"id": row_id, "run_id": run.id})
        conn.commit()

    history = runs.load_history(run.id, settings.actor_id)
    message_history = ModelMessagesTypeAdapter.validate_json(json.dumps(history))

    effects = agent_module.RunEffects()
    deps = agent_module.TrellisDeps(
        actor_id=settings.actor_id, run_id=run.id, effects=effects
    )
    runtime = agent if agent is not None else agent_module.get_agent()

    try:
        result = asyncio.run(
            runtime.run(prompt, message_history=message_history, deps=deps)
        )
    except Exception:
        return _handle_execution_failure(row, run.id, effects, access_token)

    runs.save_history(
        run.id, json.loads(ModelMessagesTypeAdapter.dump_json(result.all_messages()))
    )
    usage = result.usage
    runs.record_usage(
        run.id,
        model_calls=usage.requests,
        tool_calls=usage.tool_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )

    if isinstance(result.output, DeferredToolRequests):
        return _handle_approval(row, run.id, result.output, access_token)

    runs.set_status(run.id, RunStatus.COMPLETED)
    delivery_error = _emit(
        access_token, session_id, {"type": "response", "body": str(result.output)}
    )
    _complete_and_advance(
        row_id,
        run_id=run.id,
        organization_id=row["organization_id"],
        agent_session_id=session_id,
        last_error=delivery_error or ack_error,
    )
    return {
        "outcome": OUTCOME_COMPLETED,
        "row_id": row_id,
        "run_id": run.id,
        "delivery_error": delivery_error,
    }


def _handle_approval(
    row: dict, run_id: UUID, output: DeferredToolRequests, access_token: str
) -> dict:
    """An approval-required call, handed to the existing deterministic bridge.

    T16's boundary is neither weakened nor bypassed. `agent._open_approval`
    writes the authoritative pending row and moves the run to
    `awaiting_approval` exactly as the AG-UI path does, and the human decides in
    Trellis. Native approval continuation from inside Linear is outside the
    minimum T00W worker, so this says so in the session rather than
    auto-approving destructive work.
    """
    row_id = row["id"]
    try:
        agent_module._open_approval(run_id, output)
    except Exception as exc:
        reason = type(exc).__name__
        runs.set_status(run_id, RunStatus.FAILED, reason)
        _emit(
            access_token,
            row["agent_session_id"],
            {"type": "error", "body": FAILURE_BODY},
        )
        _fail(row_id, run_id=run_id, last_error=reason)
        return {"outcome": OUTCOME_FAILED, "row_id": row_id, "error": reason}

    delivery_error = _emit(
        access_token,
        row["agent_session_id"],
        {"type": "elicitation", "body": APPROVAL_BODY},
    )
    _complete_without_advancing(
        row_id, run_id=run_id, last_error=delivery_error or ERROR_APPROVAL_REQUIRED
    )
    return {"outcome": OUTCOME_APPROVAL_REQUIRED, "row_id": row_id, "run_id": run_id}


def _handle_execution_failure(
    row: dict, run_id: UUID, effects, access_token: str
) -> dict:
    """The model or a tool raised. Whether a retry is safe depends on one bit.

    `RunEffects.mutation_committed` is set by the tool wrappers in `agent.py`
    after a domain transaction commits. If it is set, a second attempt would
    apply that mutation twice, so the row is terminal here even though the cause
    may have been transient. If it is clear, nothing was committed and the row
    is released for another attempt with its run link cleared.

    The exception is never interpolated into the durable error. A model or
    provider exception can carry the request body, and the request body of a
    model call contains the rendered prompt.
    """
    row_id = row["id"]

    if effects.mutation_committed:
        runs.set_status(run_id, RunStatus.FAILED, ERROR_MUTATION_COMMITTED)
        _emit(
            access_token,
            row["agent_session_id"],
            {"type": "error", "body": PARTIAL_BODY},
        )
        _fail(row_id, run_id=run_id, last_error=ERROR_MUTATION_COMMITTED)
        return {
            "outcome": OUTCOME_FAILED,
            "row_id": row_id,
            "run_id": run_id,
            "error": ERROR_MUTATION_COMMITTED,
        }

    runs.set_status(run_id, RunStatus.FAILED, ERROR_MODEL_FAILED)
    _release(
        row_id,
        backoff_seconds=_backoff_seconds(row["attempt_count"]),
        last_error=ERROR_MODEL_FAILED,
    )
    return {"outcome": OUTCOME_RELEASED, "row_id": row_id, "error": ERROR_MODEL_FAILED}
