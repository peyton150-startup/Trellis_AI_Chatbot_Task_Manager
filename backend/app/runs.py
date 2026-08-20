"""Run record lifecycle and server-owned history, from BUILD_SPEC section 9.

This module is the only place that resolves a run identifier to a run, and the
only place that reads or writes `agent_runs.message_history`. Section 9 states
the second rule directly: "A single function, `runs.load_history(run_id)`, is
the only source of history anywhere in the codebase; no other code path
constructs a message list from a request."

The first rule is what makes a browser-supplied run id safe to accept. `load`
resolves the id against `agent_runs` and refuses unless the row exists and
belongs to `actor_id`. Both failures raise the same `OutOfScopeError`, matching
the architectural invariant that a missing resource and another actor's resource
are indistinguishable from outside. `SELECT_RUN` carries both predicates, so a
caller cannot resolve a run without also asserting ownership.

`load_history` deliberately takes no `actor_id`, because section 9 fixes its
signature. Callers resolve first with `load` and then read history by id. Every
route in `main.py` does exactly that.

Approval reads live here rather than in a module of their own. Section 10's tool
body names `approvals.load(...)`, but section 3's file tree has no `approvals.py`
and no task creates one, while `INSERT_APPROVAL`, `SELECT_APPROVAL`, and
`DECIDE_APPROVAL` have sat in `sql.py` without a caller since T02. Approval rows
are run-scoped control state, so `runs.load_approval` is where they belong and
section 10 is amended to name it. See D-42.
"""

from datetime import timedelta
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from psycopg.types.json import Json

from app import sql
from app.config import settings
from app.db import pool
from app.errors import ApprovalAlreadyDecidedError, OutOfScopeError
from app.models import (
    AgentRun,
    Approval,
    ApprovalDecision,
    EventOperation,
    PendingApproval,
    RunDetail,
    RunStatus,
    RunStep,
    RunUsage,
    TaskEvent,
    ToolName,
    ToolStepStatus,
    UndoResult,
)


# A run is eligible to attempt undo only after it has stopped producing effects.
# D-44. `running` and `awaiting_approval` are excluded because either can still
# commit another tool call, and compensating a live run races its own
# continuation. `failed` and `interrupted` stay eligible because both can have
# committed tools before the run stopped.
UNDOABLE_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.INTERRUPTED}
)


# D-76. What `agent_runs.model` records for a turn that no model executed.
#
# `model` is free text and this needs no migration, but the value is a claim
# about who acted, so it is named here rather than spelled at the call site. A
# control turn makes zero provider requests, so recording `settings.model_id`
# would put NVIDIA's name on work NVIDIA never did.
CONTROL_TURN_MODEL = "trellis-control"


# D-76. The three ways a run can fail to be a well-defined undo target, as
# distinct from the ways the kernel can refuse an attempt that was well defined.
#
# Deliberately not a `UndoReason`. Section 6's vocabulary and `UndoReason` both
# describe outcomes that reach a client, and nothing here does: these values
# choose which sentence the deterministic control turn writes. Adding members to
# a wire enum to carry an internal distinction is how a closed vocabulary stops
# being closed.
class UndoIneligibility(str, Enum):
    """Why an undo attempt on this run is not well defined."""

    # `running` or `awaiting_approval`. Either can still commit another tool
    # call, so compensating races the run's own continuation. D-44.
    STILL_ACTIVE = "still_active"

    # The run committed no task mutation, so there is nothing to compensate.
    # A control run is always in this state, which is what stops a second
    # "undo that" from becoming a redo of the first.
    NO_EFFECTS = "no_effects"

    # The run already carries a compensation wave. D-38: a second pass would
    # load both waves together and undo a combined history that inverts
    # nothing.
    ALREADY_COMPENSATED = "already_compensated"


@dataclass(frozen=True)
class UndoAttempt:
    """The authoritative outcome of one undo attempt on one run.

    Three outcomes, and they are not interchangeable:

        ineligible is not None   the attempt was never well defined
        result.refused           the kernel refused on current task state
        result.applied > 0       compensation committed

    `result` is None exactly when `ineligible` is set, because an ineligible
    attempt never reaches the kernel and therefore has no kernel outcome to
    report. A caller that reported "refused" for both would lose the distinction
    between "this run cannot be undone" and "this run could not be undone right
    now", which are different sentences to a human.
    """

    ineligible: UndoIneligibility | None = None
    result: UndoResult | None = None

    @property
    def applied(self) -> int:
        return 0 if self.result is None else self.result.applied


class RunAlreadyTerminalError(RuntimeError):
    """A one-way status transition found the run was already terminal.

    Not a section 6 client-facing rejection. It means two writers raced for the
    same run's outcome, and the loser must not overwrite the winner.
    """

    def __init__(self, run_id: UUID) -> None:
        super().__init__(f"run {run_id} is already terminal")
        self.run_id = run_id


@dataclass(frozen=True)
class CreatedTurn:
    """What creating a turn commits: the new run id and its starting history.

    Deliberately not an `AgentRun`. A partially populated run model would
    invite callers to read fields the write never returned.
    """

    id: UUID
    message_history: list


def _load_owned_run(conn, run_id: UUID, actor_id: UUID) -> AgentRun:
    """Resolve one actor-owned run through an existing connection.

    Missing and foreign identifiers deliberately have the same result.
    """
    row = conn.execute(
        sql.SELECT_RUN,
        {"run_id": run_id, "actor_id": actor_id},
    ).fetchone()
    if row is None:
        raise OutOfScopeError()
    return AgentRun.model_validate(row)


def create(actor_id: UUID, prompt: str, model: str) -> AgentRun:
    """Open a root run. The server issues the id; no caller may supply one."""
    with pool.connection() as conn:
        row = conn.execute(
            sql.INSERT_RUN,
            {"actor_id": actor_id, "prompt": prompt, "model": model},
        ).fetchone()
        conn.commit()
    return AgentRun.model_validate(row)


def create_turn(
    actor_id: UUID,
    prompt: str,
    model: str,
    continuity_run_id: UUID | None,
) -> CreatedTurn:
    """Create one ordinary AG-UI turn under D-67.

    Without a continuity locator this creates a root run.

    With a locator, predecessor ownership, completed-state eligibility,
    canonical-history selection, and successor creation are one statement.
    PostgreSQL reads the predecessor's history and writes the successor row
    without that history making a round trip through this process: the
    predicate list is the authority, and a predecessor that is missing, owned
    by another actor, or not completed simply matches no row, so nothing is
    inserted and the refusal is identical for all three.

    The successor is committed with inherited history already present before
    model execution can begin, exactly as before.

    Returns the committed creation result rather than a full run. Callers need
    the new id and the canonical history the model starts from, and both come
    back from the write itself, so neither has to be read again. This is
    PostgreSQL's committed creation result, not a second history authority:
    `load_history` remains the way to read an already-existing run.
    """
    with pool.connection() as conn:
        if continuity_run_id is None:
            row = conn.execute(
                sql.INSERT_RUN,
                {
                    "actor_id": actor_id,
                    "prompt": prompt,
                    "model": model,
                },
            ).fetchone()
        else:
            row = conn.execute(
                sql.INSERT_RUN_INHERITING_HISTORY,
                {
                    "actor_id": actor_id,
                    "prompt": prompt,
                    "model": model,
                    "continuity_run_id": continuity_run_id,
                },
            ).fetchone()
            if row is None:
                # No row matched the predicates. Missing, foreign, and
                # not-completed are one refusal, as they were when this was a
                # separate SELECT.
                raise OutOfScopeError()

        conn.commit()

    return CreatedTurn(
        id=row["id"],
        message_history=list(row["message_history"]),
    )


def create_control_turn(
    actor_id: UUID,
    prompt: str,
    history_predecessor_run_id: UUID | None,
) -> CreatedTurn:
    """Create one D-76 deterministic control turn. No provider executes.

    Structurally identical to `create_turn`, and separate from it for one
    reason: the model column. An ordinary turn records `settings.model_id`
    because that model is about to run. A control turn records
    `CONTROL_TURN_MODEL`, because none does, and writing the NVIDIA model id on a
    row that never made a provider request would make `agent_runs` claim
    something that did not happen. The Run Inspector reads that column, and an
    audit row that lies about who acted is worse than one that is unfamiliar.

    Parameterising `create_turn` with a model argument was the alternative and is
    worse: "which runs are control runs" would then be answered by whatever each
    caller happened to pass, rather than by one function a reader can grep.

    `history_predecessor_run_id` is deliberately not called `continuity_run_id`.
    D-76 splits the two authorities that name used to conflate. The predecessor
    supplies canonical conversation history and nothing else. It is not the undo
    target, it confers no undo authority, and the caller resolves it separately.
    The D-67 refusal is unchanged and still one refusal for missing, foreign, and
    not-completed.
    """
    with pool.connection() as conn:
        if history_predecessor_run_id is None:
            row = conn.execute(
                sql.INSERT_RUN,
                {
                    "actor_id": actor_id,
                    "prompt": prompt,
                    "model": CONTROL_TURN_MODEL,
                },
            ).fetchone()
        else:
            row = conn.execute(
                sql.INSERT_RUN_INHERITING_HISTORY,
                {
                    "actor_id": actor_id,
                    "prompt": prompt,
                    "model": CONTROL_TURN_MODEL,
                    "continuity_run_id": history_predecessor_run_id,
                },
            ).fetchone()
            if row is None:
                raise OutOfScopeError()

        conn.commit()

    return CreatedTurn(
        id=row["id"],
        message_history=list(row["message_history"]),
    )


def load(run_id: UUID, actor_id: UUID) -> AgentRun:
    """Resolve a run id to a run this actor owns, or refuse.

    A nonexistent run and another actor's run remain externally
    indistinguishable.
    """
    with pool.connection() as conn:
        run = _load_owned_run(conn, run_id, actor_id)
        conn.commit()
    return run


def assert_owned(run_id: UUID, actor_id: UUID) -> None:
    """Prove this actor owns this run, and read nothing else.

    Callers that only need the ownership answer used to reach it through
    `load`, which materializes the whole run including its entire message
    history. The refusal is identical: the predicate is the same one
    `SELECT_RUN` carries, so a missing run and another actor's run both return
    no row and both raise `OutOfScopeError`. Only the amount of state read to
    reach that answer changes.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.SELECT_RUN_OWNERSHIP,
            {"run_id": run_id, "actor_id": actor_id},
        ).fetchone()
        conn.commit()
    if row is None:
        raise OutOfScopeError()


def load_history(run_id: UUID, actor_id: UUID) -> list:
    """The single source of message history in the codebase. See section 9.

    Section 9 writes this as `runs.load_history(run_id)`. It takes `actor_id`
    as well, because history is server-owned run state and reading it means
    resolving the run first, which is an ownership question. D-15 set the
    precedent: section 10's `policy.check(...)` line likewise omitted arguments
    the function requires, and the signature was corrected rather than the
    check weakened. The property section 9 is protecting is that exactly one
    function reads history, and that is unaffected.

    Nothing else in the codebase constructs a message list, and no request model
    carries one.

    The read selects the history column alone rather than the whole run. The
    ownership predicate is unchanged and still carried by the statement, so a
    missing run and another actor's run remain the same refusal: both return no
    row, and both raise `OutOfScopeError` here. Selecting one column narrows
    what crosses the wire, not who may read it.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.SELECT_RUN_HISTORY,
            {"run_id": run_id, "actor_id": actor_id},
        ).fetchone()
        conn.commit()
    if row is None:
        raise OutOfScopeError()
    return list(row["message_history"])


def save_history(run_id: UUID, message_history: list) -> None:
    """Replace the server-owned history for a run."""
    with pool.connection() as conn:
        conn.execute(
            sql.UPDATE_RUN_HISTORY,
            {"run_id": run_id, "message_history": Json(message_history)},
        )
        conn.commit()


def complete_control_turn(run_id: UUID, message_history: list) -> AgentRun:
    """Persist a control turn's history and completion as one transaction.

    D-76, corrected after neutral review. These were two calls, and therefore
    two transactions, and the gap between them was a state the documentation
    said could not happen: `save_history` commits on its own connection, so a
    failure in the following `set_status(COMPLETED)` left a FAILED run carrying
    a fully formed "Undone." transcript. Nothing was corrupted, but a surface
    reading that run would show a coherent success narrative under a failure
    banner, and the note claiming a failed control turn has empty history was
    simply false for that ordering.

    Writing both in one statement removes the ordering rather than documenting
    it. Either the turn is completed with its history, or neither landed.

    The invariant that follows is "unchanged since creation", not "empty", and
    the difference matters because a control turn is not always born empty. A
    root turn is created with no history; a turn inheriting a completed
    predecessor is created carrying that predecessor's history, and losing it on
    failure would be its own defect.

        root turn        stays []
        inherited turn   stays H

    What must never appear is a partial control exchange. The synthetic user
    message and response land only in the same transaction that transitions the
    run to `completed`.

    `AND status = 'running'` makes the transition one way, so this cannot
    overwrite a run that already reached a terminal status. Zero rows means
    something else finished it first, and that is `RunAlreadyTerminalError`
    rather than a silent rewrite.

    This is deliberately not merged with the undo kernel's transaction. That
    would put a compensating mutation and a conversation transcript in one
    atomic unit, which is the durable-journal design D-76 declines to attempt.
    The window between the kernel's commit and this one stays open, and is
    documented and injected rather than claimed closed.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.COMPLETE_CONTROL_TURN,
            {"run_id": run_id, "message_history": Json(message_history)},
        ).fetchone()
        conn.commit()
    if row is None:
        raise RunAlreadyTerminalError(run_id)
    return AgentRun.model_validate(row)


def fail_run_if_running(run_id: UUID, error: str) -> AgentRun | None:
    """Record a terminal failure, but never over a run that already finished.

    D-76. `set_status` moves a run to any status unconditionally, which is right
    for the paths that own the whole lifecycle. This is for cleanup, which does
    not: a late failure written on top of a committed completion would make the
    run record contradict the work that actually happened.

    Returns None when the run was already terminal. That is an answer, not an
    error, and the caller is expected to leave it alone.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.FAIL_RUN_IF_RUNNING,
            {"run_id": run_id, "error": error},
        ).fetchone()
        conn.commit()
    return None if row is None else AgentRun.model_validate(row)


def set_status(run_id: UUID, status: RunStatus, error: str | None = None) -> AgentRun:
    """Move a run's status. UPDATE_RUN_STATUS stamps ended_at on terminal moves."""
    with pool.connection() as conn:
        row = conn.execute(
            sql.UPDATE_RUN_STATUS,
            {"run_id": run_id, "status": status.value, "error": error},
        ).fetchone()
        conn.commit()
    return AgentRun.model_validate(row)


def record_usage(
    run_id: UUID,
    *,
    model_calls: int = 0,
    tool_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_cents: float = 0.0,
) -> AgentRun:
    """Accumulate usage. UPDATE_RUN_USAGE adds rather than replaces, because one
    application run can contain several model invocations. See section 10."""
    with pool.connection() as conn:
        row = conn.execute(
            sql.UPDATE_RUN_USAGE,
            {
                "run_id": run_id,
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_cents": cost_cents,
            },
        ).fetchone()
        conn.commit()
    return AgentRun.model_validate(row)


def attempt_run_undo(run_id: UUID, actor_id: UUID) -> UndoAttempt:
    """D-76. The one authoritative way to undo a run. Serialized, actor scoped.

    Every surface that offers undo calls this: the D-76 control turn today, and
    T18's presentation entry point later. Neither may re-derive undoability, and
    that is the whole reason this function exists rather than each caller pairing
    `detail().can_undo` with `undo.undo_run`.

    That pairing is a TOCTOU boundary. `_can_undo` reads without locking, and
    the kernel deliberately interprets a `restored` event rather than refusing
    one, so two concurrent callers can both observe an eligible run and both
    reach the kernel.

        1. `SELECT_RUN_FOR_UNDO` takes a row lock on the actor-owned run.
        2. Status, the event wave, and the compensation check are evaluated
           inside that lock, through the same `_undo_eligibility` predicate
           `RunDetail.can_undo` reports.
        3. `undo.undo_run_on_conn` runs on this same connection, so the lock is
           still held across the kernel's precheck, apply pass, and commit.

    **What the lock is load bearing for is narrower than it looks, and the
    author mutation audit is what established that.** Dropping `FOR UPDATE` left
    the whole suite green, because safety does not actually rest on it:

        Safety
            the kernel's own guards ensure at most one compensation applies. A
            delete-undo collides on the primary key, an update-undo on the
            version guard, and a create-undo finds its rows already gone.

        Serialization
            `FOR UPDATE` ensures every competing loser observes the
            authoritative post-compensation state and therefore receives the
            correct refusal reason.

    Both are required, and the second is not cosmetic. Under the lock a loser
    reports ALREADY_COMPENSATED, which is true. Without it a loser reaches the
    kernel and refuses ROW_RECREATED, and the control turn then tells a human
    that someone else recreated their task: a false explanation of a correct
    outcome. The concurrency regression asserts the losers' reason, not merely
    their count, which is why that mutation is now caught.

    Missing and foreign remain one refusal, raised as `OutOfScopeError` before
    anything about the run is disclosed.

    Eligibility reads `SELECT_RUN_EFFECT_SUMMARY` rather than the event wave.
    The kernel is about to load that wave in full, and loading it here only to
    ask whether it is empty would double the largest read on this path.

    `undo` is imported inside the function on purpose. It imports `domain`,
    which imports the pool, and `runs` is imported by modules that must stay
    importable without a live database.
    """
    from . import undo

    with pool.connection() as conn:
        row = conn.execute(
            sql.SELECT_RUN_FOR_UNDO,
            {"run_id": run_id, "actor_id": actor_id},
        ).fetchone()
        if row is None:
            conn.rollback()
            raise OutOfScopeError()

        summary = conn.execute(
            sql.SELECT_RUN_EFFECT_SUMMARY, {"run_id": run_id}
        ).fetchone()

        ineligible = _undo_eligibility(
            RunStatus(row["status"]),
            has_events=summary["has_events"],
            has_compensation=summary["has_compensation"],
        )
        if ineligible is not None:
            conn.rollback()
            return UndoAttempt(ineligible=ineligible)

        # The kernel ends this transaction on every path, and the lock is held
        # until it does. Nothing may commit or roll back around it.
        result = undo.undo_run_on_conn(run_id, actor_id, conn=conn)

    return UndoAttempt(result=result)


def _undo_eligibility(
    status: RunStatus, *, has_events: bool, has_compensation: bool
) -> UndoIneligibility | None:
    """The single definition of whether an undo attempt is well defined.

    Read by `_can_undo`, which projects it into `RunDetail` for a surface to
    render, and by `attempt_run_undo`, which evaluates it under a row lock and
    acts on it. One predicate, two callers, so a surface can never offer an undo
    the authoritative path would refuse for a reason the surface does not know
    about.

    It is not a promise the attempt will succeed. The kernel can still refuse
    with ROW_DISAPPEARED, VERSION_CONFLICT, ROW_RECREATED, or EXTERNALLY_MODIFIED
    when current task state has moved underneath the run, and that judgment stays
    in `undo.py` where the state is actually read.
    """
    if status not in UNDOABLE_STATUSES:
        return UndoIneligibility.STILL_ACTIVE
    if not has_events:
        return UndoIneligibility.NO_EFFECTS
    if has_compensation:
        return UndoIneligibility.ALREADY_COMPENSATED
    return None


def load_approval(run_id: UUID, tool_call_id: str) -> Approval | None:
    """Load one approval row, or None when the tool is ungated.

    Section 10 step 2 calls this before `policy.check`, which takes the row as a
    parameter and never writes one. Creation and decision belong to T12B, which
    owns the approval bridge.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.SELECT_APPROVAL,
            {"run_id": run_id, "tool_call_id": tool_call_id},
        ).fetchone()
        conn.commit()
    return None if row is None else Approval.model_validate(row)


def load_owned_tasks(actor_id: UUID, task_ids: list[UUID]) -> list[dict]:
    """Complete owned task rows by id, for the T12B approval preview only.

    This is not the scope check. `policy.resolve_scope` is, and it runs before
    this function is called at all, because the disclosure the preview guard
    prevents happens the moment a foreign title is read, not when a mutation is
    attempted. The `owner_id` predicate in the statement is defence in depth
    behind that, not a substitute for it.
    """
    if not task_ids:
        return []
    with pool.connection() as conn:
        rows = conn.execute(
            sql.SELECT_TASKS_BY_IDS_FOR_OWNER,
            {"owner_id": actor_id, "task_ids": task_ids},
        ).fetchall()
        conn.commit()
    return [dict(row) for row in rows]


def open_approval(
    run_id: UUID,
    *,
    tool_call_id: str,
    tool_name: str,
    arguments: dict,
    arguments_hash: str,
    required_reason: str,
    preview: dict,
) -> Approval:
    """Write the pending approval row and move the run to `awaiting_approval`.

    Both writes happen in one transaction, and that is the whole point of this
    function existing rather than two calls at the call site. BUILD_SPEC section
    10's bridge requires the database row to exist before application state
    claims there is a pending approval, and D-57 makes `awaiting_approval`
    equivalent to "approval-controlled execution has not continued". A run left
    in `awaiting_approval` with no row, or carrying a row while still `running`,
    would be a state no reader of `RunDetail` could interpret.

    The row is written before the interrupt is presented as real. Nothing in the
    AG-UI stream is the approval record; the interrupt is a UI event and this row
    is the authorization state. See D-06.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.INSERT_APPROVAL,
            {
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": Json(arguments),
                "arguments_hash": arguments_hash,
                "required_reason": required_reason,
                "preview": Json(preview),
                "approval_ttl_seconds": settings.approval_ttl_seconds,
            },
        ).fetchone()
        conn.execute(
            sql.UPDATE_RUN_STATUS,
            {
                "run_id": run_id,
                "status": RunStatus.AWAITING_APPROVAL.value,
                "error": None,
            },
        )
        conn.commit()
    return Approval.model_validate(row)


def load_pending_approval(run_id: UUID) -> Approval | None:
    """The live card for one run, or None.

    Pending and unexpired, because a decided, denied, or expired row must not
    masquerade as something the user can still act on. D-56 freezes at most one
    simultaneously pending approval per application run, so this returns at most
    one row by invariant rather than by `LIMIT`.

    More than one row means that invariant has been violated outside this
    application, since `open_approval` is the only writer and it refuses to
    create a second. Raising is the honest response: selecting one of them to
    render is exactly the first-row-wins rule D-45 names and rejects, and no code
    in the section 6 vocabulary describes a database that contradicts its own
    invariant. This is a 500 by design and is not a client-facing rejection.
    """
    with pool.connection() as conn:
        rows = conn.execute(
            sql.SELECT_PENDING_APPROVALS_FOR_RUN, {"run_id": run_id}
        ).fetchall()
        conn.commit()
    if not rows:
        return None
    if len(rows) > 1:
        raise RuntimeError(
            f"run {run_id} holds {len(rows)} pending approvals; D-56 permits one"
        )
    return Approval.model_validate(rows[0])


def decide_approval(
    run_id: UUID, tool_call_id: str, decision: ApprovalDecision
) -> Approval:
    """Persist the human decision through the guarded update, or refuse.

    `DECIDE_APPROVAL` carries `AND decision = 'pending'` and that predicate is
    the concurrency boundary, not a convenience. Replacing it with a SELECT that
    observes `pending` followed by an unguarded UPDATE is the check-then-act race
    section 7 calls the bug rather than a style preference: two racing decision
    requests would both observe a pending row and both proceed, and the second
    would silently overwrite the first human answer.

    Zero rows back therefore means another request already decided it, and that
    is `APPROVAL_ALREADY_DECIDED` rather than a retry. The caller has already
    read the row and found it pending; this refusal is the one that survives a
    race the read cannot see.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.DECIDE_APPROVAL,
            {
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "decision": decision.value,
            },
        ).fetchone()
        conn.commit()
    if row is None:
        raise ApprovalAlreadyDecidedError()
    return Approval.model_validate(row)


def resolve_continuation(tool_call_id: str, actor_id: UUID) -> Approval:
    """D-51's mapping: a call id from `resume[].interruptId` to its approval row.

    The identifier arriving from the browser is a lookup key and grants nothing,
    in exactly the sense `{id}` is on `GET /api/runs/{id}`. What makes the
    continuation authoritative is that the row it selects already carries a
    decision this server persisted.

    `tool_call_id` is provider generated and `approvals` is keyed
    `(run_id, tool_call_id)`, so it is unique within a run and not across runs.
    D-51 requires all three cardinalities to be distinguished and forbids
    assuming uniqueness from entropy, so the statement carries no `LIMIT` and
    this function counts what came back:

        0 rows   -> refuse. No eligible approval, including the forged case
                    where nothing was ever deferred under this id.
        1 row    -> resolve it and its application run.
        >1 rows  -> refuse as ambiguous. Never choose the first.

    Both refusals raise the same `OutOfScopeError` the run resolver uses, so a
    caller cannot use the response to learn whether a call id exists somewhere
    it does not own.
    """
    with pool.connection() as conn:
        rows = conn.execute(
            sql.SELECT_APPROVAL_FOR_CONTINUATION,
            {"tool_call_id": tool_call_id, "actor_id": actor_id},
        ).fetchall()
        conn.commit()
    if len(rows) != 1:
        raise OutOfScopeError()
    return Approval.model_validate(rows[0])


def detail(run_id: UUID, actor_id: UUID) -> RunDetail:
    """Assemble the section 9 RunDetail for one resolved run.

    `pending_approval` was null from T08 until T12B, because nothing wrote an
    approval row and D-45 left the multiple-approval question open. D-56 settles
    it by freezing at most one simultaneously pending approval per application
    run, so the singular wire shape describes the state exactly rather than
    summarising a list.

    Null does not mean "not awaiting approval". Under D-57 the status spans from
    the interrupt to the end of the continuation, so a run that is
    `awaiting_approval` with a decision already persisted correctly reports a
    null card: the field means something needs your decision now, and in that
    window nothing does.

    The card deliberately carries no `arguments` and no `arguments_hash`.
    `PendingApproval` exposes the call id, tool, reason, preview, and expiry, and
    the browser needs nothing else to render a decision it does not own.
    """
    run = load(run_id, actor_id)
    pending = load_pending_approval(run_id)
    with pool.connection() as conn:
        invocations = conn.execute(
            sql.SELECT_INVOCATIONS_FOR_RUN, {"run_id": run_id}
        ).fetchall()
        events = conn.execute(
            sql.SELECT_ALL_EVENTS_FOR_RUN, {"run_id": run_id}
        ).fetchall()
        conn.commit()

    return RunDetail(
        id=run.id,
        status=run.status,
        prompt=run.prompt,
        pending_approval=None if pending is None else _pending_card(pending),
        steps=[_step(row) for row in invocations],
        usage=RunUsage(
            model_calls=run.model_calls,
            tool_calls=run.tool_calls,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            cost_cents=float(run.cost_cents),
        ),
        can_undo=_can_undo(run, [TaskEvent.model_validate(row) for row in events]),
        error=run.error,
    )


def _pending_card(approval: Approval) -> PendingApproval:
    """The browser-facing projection of one pending approval row.

    A narrowing, not a rename. `Approval` carries `arguments` and
    `arguments_hash`; `PendingApproval` does not, and section 9's wire shape
    omits them on purpose. The client renders a decision it has no authority
    over, so handing it the exact mutation arguments and the hash the tool body
    will verify against would give it everything needed to reason about the
    check rather than simply answer the question.
    """
    return PendingApproval(
        tool_call_id=approval.tool_call_id,
        tool_name=approval.tool_name,
        required_reason=approval.required_reason,
        preview=approval.preview,
        expires_at=approval.expires_at,
    )


def _can_undo(run: AgentRun, events: list[TaskEvent]) -> bool:
    """D-44. Eligibility to attempt undo, not a promise that undo will succeed.

    The precheck in `undo.py` can still refuse with ROW_DISAPPEARED,
    VERSION_CONFLICT, or ROW_RECREATED when current state has moved underneath
    the run. This predicate answers only whether an attempt is well defined.

    The third clause is what D-38 requires. Undo is a single application to an
    eligible run, and compensation events carry the original run_id, so a second
    call would load the original wave and its own compensations together and
    undo a combined history that inverts nothing. `undo.py` processes a restored
    event rather than refusing it, by design, so this is the only thing standing
    between the demo and a second undo. T18 enforces the same predicate before
    calling the kernel.
    """
    return (
        _undo_eligibility(
            run.status,
            has_events=bool(events),
            has_compensation=any(
                event.operation is EventOperation.RESTORED for event in events
            ),
        )
        is None
    )


def _step(row) -> RunStep:
    """One durable tool_invocations row as a section 9 RunStep.

    `deduplicated` is structurally unreachable here, and that is a property of
    the persistence model rather than an omission. `idempotency.acquire` returns
    REPLAY straight off a read of a completed row and writes nothing, so a
    successful replay leaves no trace to render. Every path that does rewrite
    the row, failed reacquisition and expired-lease theft, increments `attempt`,
    which is why `attempt` below is truthful. See D-43.
    """
    status = ToolStepStatus(row["status"])
    return RunStep(
        tool_call_id=row["tool_call_id"],
        tool_name=ToolName(row["tool_name"]),
        attempt=row["attempt"],
        status=status,
        duration_ms=_duration_ms(row, status),
        error=row["error"],
    )


def _duration_ms(row, status: ToolStepStatus) -> int:
    """Elapsed time of the most recent persisted attempt. D-43.

    `created_at` is the wrong anchor. Neither REACQUIRE_FAILED_LEASE nor
    STEAL_EXPIRED_LEASE rewrites it, so measuring from it would charge a stolen
    lease for the dead holder's entire expiry window and report a three second
    tool as two minutes. Both statements do set `lease_expires_at` to
    `now() + ttl`, and so does INSERT_LEASE, so subtracting the configured TTL
    recovers the moment this attempt was granted.

    The branch is on `status`, not on `completed_at IS NULL`, and that is
    load bearing. REACQUIRE_FAILED_LEASE returns a row to `pending` and bumps
    `attempt` without clearing `completed_at`, so a reacquired row is pending
    while still carrying the previous attempt's completion stamp. Branching on
    the timestamp would take the terminal path, subtract a later anchor from an
    earlier stamp, and report a negative duration.

    Limitation: the reconstruction assumes LEASE_TTL_SECONDS has not changed
    since this attempt acquired its lease. Changing the TTL while historical
    rows remain makes the derived start inaccurate for those rows, which is why
    the result is clamped at zero rather than allowed to go negative.
    """
    started_at = row["lease_expires_at"] - timedelta(
        seconds=settings.lease_ttl_seconds
    )
    observed_at = row["observed_at"] if status is ToolStepStatus.PENDING else row["completed_at"]
    if observed_at is None:
        return 0
    return max(0, int((observed_at - started_at).total_seconds() * 1000))
