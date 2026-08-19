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
model                    agent.get_linear_agent().run, the safe profile
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

**Linear runs a reduced safe profile, and that is a capability boundary
rather than a preference.** `agent.get_linear_agent()` registers `list_tasks`,
`get_task_history`, `resolve_task_reference`, `create_task`, `update_task`, and
`propose_plan`. The two omitted tools, `delete_tasks` and `bulk_update_tasks`,
are exactly the two that can require approval. `get_task_history` and
`resolve_task_reference` are read-only and require no approval, so D-71 and
D-73 safely expose the same durable history projection and the same
actor-scoped discovery from Linear. Linear
still has no channel that can decide an approval: a `select` elicitation returns
an ordinary user `prompt` activity, which is a new message rather than a
resumption of an interrupted invocation. An approval card raised from here would
be one no Linear answer could ever decide, so the destructive capabilities stay
withheld. T16's browser approval bridge is untouched, and native Linear approval
continuation is work T00W does not attempt.

**A control signal never becomes a prompt.** `activity_signal` is consulted
before `extract_prompt`, and a `stop` halts without a model call, without a
tool, without a run, and without any Linear call beyond the single closing
activity Linear's contract asks for. The limitation is stated where it lives:
this stops a turn that has not started, and cannot cancel one already inside
`Agent.run`.

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
import logging
import signal
import sys
import threading
from typing import Any
from uuid import UUID, uuid4

from pydantic_ai import DeferredToolRequests
from pydantic_ai.messages import ModelMessagesTypeAdapter

from . import agent as agent_module
from . import limits, linear_agent, runs, sql
from .config import settings
from .db import pool
from .linear_agent_api import LinearApiError
from .models import RunStatus


# Durable, machine-readable worker outcomes. Never prose and never a payload.
OUTCOME_IDLE = "idle"
OUTCOME_COMPLETED = "completed"
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
ERROR_PROMPT_TOO_LARGE = "prompt_exceeds_accepted_size"
ERROR_ATTEMPTS_EXHAUSTED = "attempt_budget_exhausted"
ERROR_AMBIGUOUS_TURN = "turn_already_executed_outcome_ambiguous"
ERROR_UNSUPPORTED_CAPABILITY = "model_requested_a_tool_outside_the_linear_profile"
ERROR_MUTATION_COMMITTED = "mutation_committed_response_incomplete"
ERROR_MODEL_FAILED = "model_invocation_failed"
ERROR_TOKEN_RACE_LOST = "refresh_token_race_lost"
ERROR_STOPPED = "stopped_by_user_signal"

# How far ahead of expiry a token is refreshed. Deliberately generous: Linear
# requires the first activity on a created session within ten seconds, and a
# refresh performed at the moment of expiry puts a provider round trip on that
# critical path. See `ensure_access_token`.
TOKEN_REFRESH_SKEW_SECONDS = 300

# The single-instance advisory lock key. An arbitrary but fixed 64 bit constant:
# PostgreSQL advisory locks are keyed by number, not by name, so the value only
# has to be stable and not collide with another advisory lock in this database.
# Nothing else in this build takes one.
WORKER_ADVISORY_LOCK_KEY = 7700_2026_0818

# The signal Linear sends when a human stops the agent. Handled before anything
# else, and never as prompt text. See `_handle_signal`.
SIGNAL_STOP = "stop"

# What the human sees in Linear. Fixed text, because these are the worker's own
# words and never an echo of a payload or of an exception.
ACK_BODY = "Working on it. Checking your Trellis tasks."
UNSUPPORTED_BODY = (
    "That request needs a capability I do not have from Linear. Deleting tasks "
    "and bulk updates require approval, and approval is decided in the Trellis "
    "app. Nothing was changed."
)
STOPPED_BODY = "Stopped. I have taken no further action."
UNSUPPORTED_SIGNAL_BODY = (
    "I received a control signal I do not handle, so I stopped without acting."
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


class PromptTooLarge(Exception):
    """D-74. An extractable prompt past the ceiling for its action.

    Distinct from returning None, which means no prompt could be found at
    all. Conflating them would record "no_extractable_prompt" for a row that
    carried a perfectly extractable prompt, and would send a Linear user
    looking for a message they can see they sent.
    """


def _text(value: Any) -> str | None:
    """A non-empty string, or None. Whitespace alone is not a prompt."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _bounded(value: str | None, ceiling: int) -> str | None:
    """The prompt, or a refusal. Never a truncation.

    Truncating would hand the model an instruction whose second half is
    missing, and it would do so silently. The ceiling is applied after
    `_text` has stripped, so surrounding whitespace never decides the answer.
    """
    if value is not None and len(value) > ceiling:
        raise PromptTooLarge(len(value))
    return value


def activity_signal(payload: dict) -> str | None:
    """The control signal on this event, or None for an ordinary message.

    Separate from `extract_prompt` and consulted before it, because a signal is
    not a degenerate prompt. Linear's schema carries `signal` and
    `signalMetadata` beside `content` precisely so that a stop is distinguishable
    from someone typing the word stop, and collapsing the two would make the
    control channel forgeable by content.
    """
    activity = payload.get("agentActivity")
    if not isinstance(activity, dict):
        return None
    signal = activity.get("signal")
    return signal if isinstance(signal, str) and signal else None


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
            # Linear's schema makes `content` a required JSONObject and types
            # the user-authored variant as `prompt`. Any other type is some
            # other activity, and treating it as a user instruction would let a
            # non-prompt activity drive the agent.
            if content.get("type") != "prompt":
                return None
            body = _text(content.get("body"))
            if body is not None:
                return _bounded(body, limits.LINEAR_PROMPTED_MESSAGE_MAX_CHARS)

        # Compatibility only. Linear's prose documents the prompted message at
        # `agentActivity.body`, so this is kept for the case where a payload
        # matches the prose rather than the schema.
        return _bounded(
            _text(activity.get("body")),
            limits.LINEAR_PROMPTED_MESSAGE_MAX_CHARS,
        )

    if action == linear_agent.ACTION_CREATED:
        # `promptContext` is the formatted string Linear assembles from the
        # mention, the issue, and the surrounding comments. It is the documented
        # location for a created session and the only field here that describes
        # the whole request rather than one fragment of it.
        context = _text(payload.get("promptContext"))
        if context is not None:
            return _bounded(context, limits.LINEAR_CREATED_CONTEXT_MAX_CHARS)

        session = payload.get("agentSession")
        if isinstance(session, dict):
            comment = session.get("comment")
            if isinstance(comment, dict):
                body = _text(comment.get("body"))
                if body is not None:
                    return _bounded(
                        body, limits.LINEAR_CREATED_CONTEXT_MAX_CHARS
                    )
            issue = session.get("issue")
            if isinstance(issue, dict):
                title = _text(issue.get("title"))
                if title is not None:
                    return _bounded(
                        title, limits.LINEAR_CREATED_CONTEXT_MAX_CHARS
                    )
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


def _read_installation(installation_id: UUID) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            sql.SELECT_ACTIVE_LINEAR_INSTALLATION_BY_ID, {"id": installation_id}
        ).fetchone()
        conn.commit()
    if row is None:
        raise _Terminal(ERROR_NO_INSTALLATION)
    return dict(row)


def _is_fresh(row: dict, now) -> bool:
    expires_at = row["access_token_expires_at"]
    return (
        expires_at is not None
        and (expires_at - now).total_seconds() > TOKEN_REFRESH_SKEW_SECONDS
    )


def ensure_access_token(installation_id: UUID) -> str:
    """The live access token for one installation, refreshed compare-and-swap.

    **No database transaction is held across the call to Linear**, which is the
    same rule `linear_install.py` follows and for the same reason: a provider
    that hangs would otherwise pin a connection and a row lock for the whole
    timeout, and this call sits on the critical path of an acknowledgement
    Linear expects within ten seconds.

    Mutual exclusion is still required, because Linear rotates refresh tokens
    and two workers that both refreshed would leave one persisting a value the
    provider has already superseded. It is obtained by compare-and-swap instead
    of by a lock: the refresh happens outside any transaction, and the write
    back is guarded on the refresh token that was actually spent. The loser of
    the race updates zero rows, learns that from the row count, and re-reads the
    winner's token rather than overwriting it.

    Linear documents a grace period in which a refresh may be replayed after a
    network failure, which is what makes the loser's spent token recoverable
    rather than merely lost. That allowance is specific to refresh and is not
    generalized into a retry anywhere else.

    `TOKEN_REFRESH_SKEW_SECONDS` is deliberately generous. A token refreshed
    only at the moment of expiry puts a provider round trip on the critical path
    of the ten second acknowledgement window every time it happens; refreshing
    well ahead of expiry means it usually happens on a turn that has time to
    spare.
    """
    row = _read_installation(installation_id)

    with pool.connection() as conn:
        now = conn.execute("SELECT now() AS now;").fetchone()["now"]
        conn.commit()

    if _is_fresh(row, now):
        return row["access_token"]

    spent = row["refresh_token"]
    if not spent:
        raise _Terminal(ERROR_NO_INSTALLATION)

    # Outside every transaction.
    try:
        tokens = provider().refresh_tokens(spent)
    except LinearApiError as exc:
        raise _Transient(_safe_provider_error(exc)) from None

    with pool.connection() as conn:
        updated = conn.execute(
            sql.ROTATE_LINEAR_INSTALLATION_TOKENS,
            {
                "id": installation_id,
                "spent_refresh_token": spent,
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "expires_in": tokens.expires_in,
            },
        ).fetchone()
        conn.commit()

    if updated is not None:
        return updated["access_token"]

    # Another worker rotated first. Its token is the live one; ours is spent.
    current = _read_installation(installation_id)
    with pool.connection() as conn:
        now = conn.execute("SELECT now() AS now;").fetchone()["now"]
        conn.commit()
    if _is_fresh(current, now):
        return current["access_token"]
    raise _Transient(ERROR_TOKEN_RACE_LOST)


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

    # Any other status, `awaiting_approval` included, is a failure here. A
    # Linear-originated run can no longer reach that state, because the Linear
    # profile registers no tool that can be deferred, so observing one means the
    # run was touched by something other than this worker. Advancing the cursor
    # to it would hand the next turn a predecessor `runs.create_turn` refuses.
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

    # Signals are resolved before anything else, because the answer to a stop is
    # to do nothing rather than to do something smaller.
    signal = activity_signal(payload)
    if signal is not None:
        return _handle_signal(row, installation, signal)

    # D-74. Over the ceiling is permanent for this row: the stored payload
    # will not shrink, so a retry reaches the same answer.
    try:
        prompt = extract_prompt(payload)
    except PromptTooLarge:
        raise _Terminal(ERROR_PROMPT_TOO_LARGE) from None
    if prompt is None:
        raise _Terminal(ERROR_NO_PROMPT)

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
    runtime = agent if agent is not None else agent_module.get_linear_agent()

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
        return _handle_unsupported_capability(row, run.id, access_token)

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


def _handle_unsupported_capability(
    row: dict, run_id: UUID, access_token: str
) -> dict:
    """A deferred request reached a profile that has no deferrable tools.

    Unreachable through `LINEAR_TOOLS`, because the only two tools that can
    require approval are not registered on the Linear agent. It is kept as a
    fail-closed branch rather than deleted, because "unreachable" is a property
    of today's profile and a later widening would otherwise land here silently.

    **No approval row is written and nothing is auto-approved.** T00W has no way
    to decide an approval from inside Linear: a `select` elicitation returns an
    ordinary user `prompt` activity, which is a new message rather than a
    resumption of the interrupted invocation, so an approval card raised here
    would be one no Linear answer could ever decide. Refusing the turn is the
    honest outcome, and it leaves T16's browser bridge untouched.
    """
    row_id = row["id"]
    runs.set_status(run_id, RunStatus.FAILED, ERROR_UNSUPPORTED_CAPABILITY)
    _emit(
        access_token,
        row["agent_session_id"],
        {"type": "error", "body": UNSUPPORTED_BODY},
    )
    _fail(row_id, run_id=run_id, last_error=ERROR_UNSUPPORTED_CAPABILITY)
    return {
        "outcome": OUTCOME_FAILED,
        "row_id": row_id,
        "run_id": run_id,
        "error": ERROR_UNSUPPORTED_CAPABILITY,
    }


def _handle_signal(row: dict, installation: dict, signal: str) -> dict:
    """A control signal, answered without a model, a tool, or a turn.

    Linear's contract for `stop` is explicit: halt, take no further actions and
    make no further API calls, then emit one final response or error. So this
    path calls no model, registers no run, touches no task, and emits exactly
    one closing activity before finalizing the row.

    **The limitation is stated rather than papered over.** This stops a turn
    that has not started. It cannot cancel a turn already executing inside
    `Agent.run`, because Trellis has no cancellation channel into a running
    invocation, and per-session serialization means a stop for a session whose
    earlier row is still leased waits behind it rather than interrupting it.
    Claiming otherwise would be claiming compliance this build does not have.

    An unrecognized signal is refused the same way rather than falling through
    to `extract_prompt`. A control message this worker does not understand is
    the last thing that should be handed to a model as an instruction.
    """
    row_id = row["id"]
    reason = ERROR_STOPPED if signal == SIGNAL_STOP else ERROR_SIGNAL_NOT_PROMPT

    try:
        access_token = ensure_access_token(installation["id"])
    except (_Terminal, _Transient):
        # No credential means no closing activity is possible. The row is still
        # finalized: a stop must not be retried into a turn later.
        _fail(row_id, run_id=None, last_error=reason)
        return {"outcome": OUTCOME_FAILED, "row_id": row_id, "error": reason}

    body = STOPPED_BODY if signal == SIGNAL_STOP else UNSUPPORTED_SIGNAL_BODY
    delivery_error = _emit(
        access_token, row["agent_session_id"], {"type": "response", "body": body}
    )
    _fail(row_id, run_id=None, last_error=delivery_error or reason)
    return {"outcome": OUTCOME_FAILED, "row_id": row_id, "error": reason}


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


# ------------------------------------------------------------- process entry

log = logging.getLogger("trellis.linear_worker")


def acquire_single_instance_lock(conn) -> bool:
    """Take the process-wide worker lock on `conn`, or report that it is held.

    `pg_try_advisory_lock` rather than `pg_advisory_lock`, because a second
    worker should exit and say so rather than block forever looking healthy to
    systemd while draining nothing.

    The lock is session scoped, so it is held for exactly as long as `conn`
    stays checked out and is released automatically if the process dies. That is
    the property a lock file does not have: a killed worker leaves no stale lock
    to clear by hand before the service will start again.

    **This is defense in depth, not the correctness mechanism.** `CLAIM_LINEAR_INBOX`
    is already safe under concurrency: `FOR UPDATE SKIP LOCKED` plus the
    earlier-pending-row predicate means two workers cannot take the same row and
    cannot reorder one session's turns. Competing workers would be correct and
    merely wasteful. This makes "exactly one drains" observable instead of
    merely tolerable, so a duplicated systemd unit or a stray manual run is
    reported at startup rather than discovered later in the model bill.
    """
    held = conn.execute(
        "SELECT pg_try_advisory_lock(%(key)s) AS held;",
        {"key": WORKER_ADVISORY_LOCK_KEY},
    ).fetchone()["held"]
    conn.commit()
    return bool(held)


def run_forever(
    *,
    poll_seconds: float | None = None,
    stop: threading.Event | None = None,
    agent=None,
) -> int:
    """Drain the inbox until asked to stop. The production loop.

    Returns a process exit status: 0 for a clean shutdown, 1 if another worker
    already holds the single-instance lock.

    `stop` is an `Event` rather than a boolean flag so the idle wait is
    interruptible. A loop that slept through its shutdown signal would make
    every deploy wait the full poll interval, and systemd would eventually
    escalate to SIGKILL mid-turn, which is exactly the crash the ambiguous-turn
    guard exists to survive rather than something to cause on purpose.

    Shutdown is checked between rows, never inside one. A turn that has started
    runs to its finalization, so the loop never leaves a claimed row leased with
    its outcome unrecorded when it can avoid it.
    """
    interval = (
        settings.linear_worker_poll_seconds if poll_seconds is None else poll_seconds
    )
    stopping = stop if stop is not None else threading.Event()

    with pool.connection() as lock_conn:
        if not acquire_single_instance_lock(lock_conn):
            log.error(
                "another Trellis Linear worker already holds the single-instance "
                "lock; exiting without draining"
            )
            return 1

        try:
            log.info("Trellis Linear worker started, poll interval %ss", interval)
            return _drain_until_stopped(stopping, interval, agent)
        finally:
            # Explicit, and not merely tidiness. The lock is scoped to the
            # database session, and `pool.connection()` returns the connection
            # to the pool rather than closing it, so the session outlives this
            # block and would carry the lock with it. A later caller borrowing
            # that same pooled connection would find the worker lock already
            # held by itself and refuse to start.
            release_single_instance_lock(lock_conn)
            log.info("Trellis Linear worker stopped")


def release_single_instance_lock(conn) -> None:
    """Hand the worker lock back. See the note in `run_forever`."""
    conn.execute(
        "SELECT pg_advisory_unlock(%(key)s);", {"key": WORKER_ADVISORY_LOCK_KEY}
    )
    conn.commit()


def _drain_until_stopped(stopping, interval, agent) -> int:
    """The loop itself, so the lock lifetime above stays one readable block."""
    while not stopping.is_set():
        try:
            result = process_next(agent=agent)
        except Exception:
            # `process_next` already converts ordinary failures into outcomes,
            # so reaching here means something unexpected. The loop must not die
            # on it: the claimed row's lease expires and the row becomes
            # reclaimable, which is the behavior the lease exists for.
            # `exception` logs the traceback, which carries no payload and no
            # credential because nothing here interpolates one.
            log.exception("unexpected worker failure; continuing")
            stopping.wait(interval)
            continue

        if result["outcome"] == OUTCOME_IDLE:
            stopping.wait(interval)
    return 0


def main() -> int:
    """`python -m app.linear_agent_worker`. The systemd entry point.

    SIGTERM is what systemd sends on `stop` and `restart`, and SIGINT is what a
    terminal sends. Both set the same event, so an operator pressing Ctrl-C and
    a deploy restarting the unit take the identical path.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    stopping = threading.Event()

    def _request_stop(signum, _frame) -> None:
        log.info("received signal %s; finishing the current turn", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    return run_forever(stop=stopping)


if __name__ == "__main__":
    sys.exit(main())
