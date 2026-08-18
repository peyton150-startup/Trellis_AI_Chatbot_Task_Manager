"""The typed tools the model operates through.

D-49 split T10 authoring: `create_task` is the corrected reference and the five
other tools transcribe its body without redesign. Every tool therefore follows
one order: canonical payload, argument hash, actor-bound completed-replay
preflight, D-12 classification, authoritative policy check, lease acquisition,
one transaction, and return.

D-50 inserts one step between the replay preflight and D-12 classification, and
only in the two tools whose classification can actually require approval:
`bulk_update_tasks`, which is conditional on the blast radius, and
`delete_tasks`, which is destructive at any count. Those two resolve actor scope
before the raise, because D-12 requires scope to be resolved before the raise
and every other tool's classification is inert, returning `required=False` at a
count of zero or one. The other four are unchanged, so the identical-body rule
in BUILD_SPEC section 10 now has a named two-tool exception rather than a silent
one. Adding the step to a tool that cannot defer would buy a second database
read and no property, and it still could not run ahead of the framework's
declarative gate on `delete_tasks`, which fires before the body at all.

The step sits after the replay preflight, not before it. Q-12 exists because a
committed delete removes its own targets, so a scope load ahead of replay would
raise `OUT_OF_SCOPE` on a byte-identical repeat and make the stored result
unreachable, which is the defect Q-12 reproduced.

The completed-replay preflight sits ahead of both policy and D-12 step 0. It is
read only and resolves run ownership before reading a lease. That makes a
committed result reachable after its target rows disappear while still ensuring
that a foreign or missing run is refused before any other work. If a pending
preflight loses a race to a committing caller and policy then sees a missing
target, the tool repeats the same actor-, tool-, and hash-bound preflight once
before preserving the scope refusal.

Scope targets and blast radius are intentionally separate. `target_ids`
contains every row whose ownership must be checked, including `blocked_by`.
`blast_radius_count` contains only rows the call mutates: zero for list, create,
and plan; one for a single update; and the supplied task count for bulk update
and delete. Both classification and the authoritative policy check receive the
same count.

Four earlier decisions remain load bearing. D-12 keeps conditional approval
ahead of lease acquisition so a deferred call takes no lease. D-15 makes the
current run and call required inputs to policy. D-18 keeps the domain mutation,
events, and lease completion on the caller's one transaction. D-42 places the
approval read in `runs.py`.

Nothing in the shared body may move `idempotency.acquire` ahead of
`policy.check`: a refused new call must take no lease. A replay returns before
domain work. For mutating tools, the domain write, its events, and completion
commit together. Read-only tools still complete a tracked invocation, while
`propose_plan` writes no task state or task event.
"""

from dataclasses import dataclass
from uuid import UUID

from pydantic_ai.exceptions import ApprovalRequired

from . import domain, idempotency, policy, runs
from .errors import OutOfScopeError
from .models import (
    BulkUpdateTasksArgs,
    CreateTaskArgs,
    DeleteTasksArgs,
    GetTaskHistoryArgs,
    LeaseAction,
    ListTasksArgs,
    MutableTaskFields,
    ProposePlanArgs,
    ToolName,
    UpdateTaskArgs,
)


# BUILD_SPEC section 11 fixes this constant and its contents. The eval
# invariant counts completed invocations of these four against the number of
# mutations actually committed, so `list_tasks` and `propose_plan` are excluded:
# the property under test is that no mutation happened twice, not that the model
# made a particular number of tool calls.
MUTATING_TOOLS = frozenset(
    {
        ToolName.CREATE_TASK.value,
        ToolName.UPDATE_TASK.value,
        ToolName.BULK_UPDATE_TASKS.value,
        ToolName.DELETE_TASKS.value,
    }
)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What section 10's `ctx` carries into every tool.

    `run_id` is the **application** run, `agent_runs.id`. Section 10 separates
    that from the framework's per-invocation run identity in terms, because one
    application run can contain several invocations when an approval interrupt
    splits it. `pydantic_ai.RunContext` also exposes a `run_id`, and it is the
    other one. Nothing in this file may read `run_id` off a `RunContext`, and
    T12A is where the adaptation happens: it builds this context from the
    framework's `tool_call_id` and `tool_call_approved` plus the application
    values it holds in its dependencies.

    Keeping the tools bound to this small frozen record rather than to
    `RunContext` is also what makes section 12's done-when reachable, that each
    tool is callable directly.
    """

    actor_id: UUID
    run_id: UUID
    tool_call_id: str
    # False is the correct default. It is read only by step 0 of a tool that can
    # require approval, and on the first pass of such a call the framework has
    # not approved anything yet.
    tool_call_approved: bool = False


def list_tasks(
    ctx: ToolContext,
    arguments: ListTasksArgs,
) -> list[domain.TaskSnapshot]:
    """Return one bounded page of owned tasks through the shared lease path."""
    tool_name = ToolName.LIST_TASKS.value
    target_ids: list[UUID] = []
    blast_radius_count = 0
    payload = _payload(arguments)

    # 1.
    args_hash = policy.arguments_hash(payload)

    # 1a.
    replayed = idempotency.replay_completed(
        ctx.run_id, ctx.tool_call_id, tool_name, args_hash, actor_id=ctx.actor_id
    )
    if replayed is not None:
        return replayed

    # 0.
    requirement = policy.classify(tool_name, payload, blast_radius_count)
    if requirement.required and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"reason": requirement.reason})

    # 2.
    approval_row = runs.load_approval(ctx.run_id, ctx.tool_call_id)
    try:
        policy.check(
            ctx.actor_id,
            tool_name,
            payload,
            target_ids,
            approval_row,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            blast_radius_count=blast_radius_count,
        )
    except OutOfScopeError:
        replayed = idempotency.replay_completed(
            ctx.run_id,
            ctx.tool_call_id,
            tool_name,
            args_hash,
            actor_id=ctx.actor_id,
        )
        if replayed is not None:
            return replayed
        raise

    # 3.
    outcome = idempotency.acquire(ctx.run_id, ctx.tool_call_id, tool_name, args_hash)
    if outcome.action is LeaseAction.REPLAY:
        return outcome.result

    # 4.
    committed = False
    try:
        with _pool().connection() as conn:
            tasks = domain.list_tasks(ctx.actor_id, arguments, conn=conn)
            domain.write_events(ctx.run_id, ctx.actor_id, (), conn=conn)
            result = [task.model_dump(mode="json") for task in tasks]
            idempotency.complete(ctx.run_id, ctx.tool_call_id, result, conn=conn)
            conn.commit()
            committed = True
    except Exception as exc:
        if not committed:
            idempotency.fail(ctx.run_id, ctx.tool_call_id, str(exc))
        raise

    # 5.
    return result


def get_task_history(
    ctx: ToolContext,
    arguments: GetTaskHistoryArgs,
) -> dict:
    """Return one actor-scoped page of durable task history.

    History authorization intentionally differs from ordinary live-task scope.
    A deleted task has no current task row, but actor-owned task_events remain
    valid ownership evidence.

    Keep target_ids empty. domain.read_task_history is the resource-specific
    authority for both current and deleted task history.

    The first history read is an authorization probe only and is discarded.
    A foreign or missing task therefore fails before lease acquisition.

    After acquire grants EXECUTE, history is read again. Only that post-lease
    value may become the invocation's stored replay result.
    """
    tool_name = ToolName.GET_TASK_HISTORY.value

    # SECURITY INVARIANT:
    # Do not add arguments.task_id here. Ordinary policy scope intentionally
    # understands live task rows only; history ownership survives deletion.
    target_ids: list[UUID] = []
    blast_radius_count = 0
    payload = _payload(arguments)

    # 1.
    args_hash = policy.arguments_hash(payload)

    # 1a. Completed replay comes before fresh authorization/domain work.
    replayed = idempotency.replay_completed(
        ctx.run_id,
        ctx.tool_call_id,
        tool_name,
        args_hash,
        actor_id=ctx.actor_id,
    )
    if replayed is not None:
        return replayed

    # 0. This is read-only, but still uses the normal classification pipeline.
    requirement = policy.classify(tool_name, payload, blast_radius_count)
    if requirement.required and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"reason": requirement.reason})

    # 2. Standard policy still runs with intentionally empty live-task targets.
    approval_row = runs.load_approval(ctx.run_id, ctx.tool_call_id)
    try:
        policy.check(
            ctx.actor_id,
            tool_name,
            payload,
            target_ids,
            approval_row,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            blast_radius_count=blast_radius_count,
        )
    except OutOfScopeError:
        replayed = idempotency.replay_completed(
            ctx.run_id,
            ctx.tool_call_id,
            tool_name,
            args_hash,
            actor_id=ctx.actor_id,
        )
        if replayed is not None:
            return replayed
        raise

    # History-specific authorization probe.
    #
    # Discard this value. acquire has not granted execution authority yet, so
    # this preflight page must never become the stored invocation result.
    with _pool().connection() as conn:
        domain.read_task_history(
            ctx.actor_id,
            arguments.task_id,
            limit=arguments.limit,
            before_event_id=arguments.before_event_id,
            conn=conn,
        )

    # 3.
    outcome = idempotency.acquire(
        ctx.run_id,
        ctx.tool_call_id,
        tool_name,
        args_hash,
    )
    if outcome.action is LeaseAction.REPLAY:
        return outcome.result

    # 4. Produce the authoritative result after owning EXECUTE.
    committed = False
    try:
        with _pool().connection() as conn:
            history = domain.read_task_history(
                ctx.actor_id,
                arguments.task_id,
                limit=arguments.limit,
                before_event_id=arguments.before_event_id,
                conn=conn,
            )
            domain.write_events(ctx.run_id, ctx.actor_id, (), conn=conn)
            result = history.model_dump(mode="json")
            idempotency.complete(
                ctx.run_id,
                ctx.tool_call_id,
                result,
                conn=conn,
            )
            conn.commit()
            committed = True
    except Exception as exc:
        if not committed:
            idempotency.fail(ctx.run_id, ctx.tool_call_id, str(exc))
        raise

    # 5.
    return result


def create_task(
    ctx: ToolContext,
    arguments: CreateTaskArgs,
) -> list[domain.TaskSnapshot]:
    """Create one task for the acting owner.

    The reference body for the original T10 six.

    Returns the created rows as JSON-safe snapshots, which is the same value
    stored on the lease. That equality is deliberate and is the reason the
    return is not a list of `Task` models: step 3 replays whatever
    `tool_invocations.result` holds, which has been through JSONB, so a first
    call returning models and a retry returning dictionaries would make the
    replay observably different from the work it stands in for.
    """
    tool_name = ToolName.CREATE_TASK.value

    # `blocked_by` is a target. It names an existing row, and without it in this
    # list the tool is an existence oracle: a foreign task id inserts cleanly
    # while a nonexistent one fails on the foreign key, so the two outcomes are
    # distinguishable and the caller learns which ids are real. It would also
    # write a cross-actor relationship, which the actor-scope invariant forbids.
    # Passing it through step 1 of `policy.check` makes a missing row and another
    # actor's row raise the identical `OutOfScopeError` before any insert.
    #
    # `policy._load_task_owners` handles the ordinary case, where `blocked_by` is
    # absent: an empty list skips the owner query and satisfies scope explicitly
    # rather than by a vacuously true comparison.
    target_ids: list[UUID] = (
        [arguments.blocked_by] if arguments.blocked_by is not None else []
    )

    # Blast radius counts rows the call mutates, and `create_task` mutates none:
    # it adds one. `blocked_by` is in `target_ids` for the ownership question
    # only. Passing the two lists separately is what keeps three task ids plus a
    # blocker from crossing a threshold of three. See `policy.check`.
    blast_radius_count = 0

    payload = _payload(arguments)

    # 1. Ahead of step 0, deliberately, and this is the one place the reference
    #    departs from D-12's printed wording. D-12 puts the approval raise ahead
    #    of `arguments_hash`, and its stated reason is the lease deadlock: a
    #    deferring pass must not take a lease it never completes. Hashing takes
    #    no lease. Leaving step 0 in front would make a completed conditional
    #    call unreplayable, because the retry would raise ApprovalRequired again
    #    rather than return the result it already committed. The deadlock reason
    #    is preserved exactly, because step 3 still runs after step 0.
    args_hash = policy.arguments_hash(payload)

    # 1a. Completed-replay preflight. Read only: it takes no lease, reacquires
    #     nothing and steals nothing, so a refused new call still takes no lease.
    #     It resolves run ownership first and refuses a foreign or missing run
    #     terminally. It sits ahead of policy because policy's scope load is
    #     exactly what makes a committed result unreachable once its target rows
    #     are gone, which any mutating tool can reach and `delete_tasks` reaches
    #     every time. See `idempotency.replay_completed`.
    replayed = idempotency.replay_completed(
        ctx.run_id, ctx.tool_call_id, tool_name, args_hash, actor_id=ctx.actor_id
    )
    if replayed is not None:
        return replayed

    # 0. D-12. Inert for this tool, and written out so all four mutating bodies
    #    are identical. For `bulk_update_tasks` this is the raise that defers the
    #    call; for `delete_tasks` the framework gate fires first and this is
    #    never reached.
    requirement = policy.classify(tool_name, payload, blast_radius_count)
    if requirement.required and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"reason": requirement.reason})

    # 2. The authoritative gate, run on every path including the approved one.
    #    Framework approval is a UI gate; the row in `approvals` is the
    #    authorization record, and checking it again here is defense in depth
    #    that must not be optimized away. `check` raises on every refusal, so the
    #    returned `PolicyDecision` is not bound: section 10 prints the binding,
    #    and an unused local is an F841 under this repository's ruff selection.
    approval_row = runs.load_approval(ctx.run_id, ctx.tool_call_id)
    try:
        policy.check(
            ctx.actor_id,
            tool_name,
            payload,
            target_ids,
            approval_row,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            blast_radius_count=blast_radius_count,
        )
    except OutOfScopeError:
        # The preflight can observe `pending` and lose a race: the caller holding
        # the lease commits and removes the target between that read and this
        # check, and scope then refuses a call whose result exists. Ask the same
        # kernel function once more rather than reinterpreting lease state here.
        # This is not the rejected per-tool lease inspection: interpretation
        # stays in `idempotency`, and the second call is still actor bound, tool
        # bound and hash bound, so it can only return a result this caller was
        # already authorized for.
        replayed = idempotency.replay_completed(
            ctx.run_id, ctx.tool_call_id, tool_name, args_hash, actor_id=ctx.actor_id
        )
        if replayed is not None:
            return replayed
        raise

    # 3. Before any mutation. A retry whose work already committed returns the
    #    stored result here and touches nothing.
    outcome = idempotency.acquire(ctx.run_id, ctx.tool_call_id, tool_name, args_hash)
    if outcome.action is LeaseAction.REPLAY:
        return outcome.result

    # 4. One transaction. The mutation, its events, and the lease completion
    #    commit together or not at all, so a pending row is proof that nothing
    #    happened, which is what makes stealing an expired lease safe.
    committed = False
    try:
        with _pool().connection() as conn:
            mutation = domain.create_task(ctx.actor_id, arguments, conn=conn)
            domain.write_events(
                ctx.run_id, ctx.actor_id, mutation.events, conn=conn
            )
            result = [task.model_dump(mode="json") for task in mutation.tasks]
            idempotency.complete(ctx.run_id, ctx.tool_call_id, result, conn=conn)
            conn.commit()
            committed = True
    except Exception as exc:
        # This is a deliberate extension of the five steps, not a transcription
        # of them, and it is sound for three reasons worth stating because Sol
        # copies this block into three more tools.
        #
        # It runs after rollback. The `with` is inside the `try`, so the context
        # manager's exit has already rolled the transaction back and returned the
        # connection to the pool before this handler runs. `fail` then takes its
        # own connection and commits, which is what section 7 requires of it and
        # why D-18 exempts it from the caller-owned-connection rule that binds
        # `complete`.
        #
        # It only ever writes a pending row. `FAIL_LEASE` is an unconditional
        # UPDATE, as `sql.py` says in terms, so calling it on a row that reached
        # `completed` would rewrite a committed result to failed. The only way to
        # raise after a successful commit is a failure inside the context
        # manager's exit, so `committed` guards it rather than assuming that
        # cannot happen.
        #
        # It leaves the row in the state T05 built a guarded path for. A failed
        # row is not terminal: `REACQUIRE_FAILED_LEASE` exists to take it, and
        # without this handler the row stays pending until its TTL expires and
        # the next retry has to steal it instead. Nothing in the application
        # called `fail` before T10, which is why the reacquire branch had only
        # the T05 gate exercising it.
        if not committed:
            idempotency.fail(ctx.run_id, ctx.tool_call_id, str(exc))
        raise

    # 5.
    return result


def update_task(
    ctx: ToolContext,
    arguments: UpdateTaskArgs,
) -> list[domain.TaskSnapshot]:
    """Apply one version-guarded task update through the shared lease path."""
    tool_name = ToolName.UPDATE_TASK.value
    target_ids = [arguments.task_id]
    if "blocked_by" in arguments.model_fields_set and arguments.blocked_by is not None:
        target_ids.append(arguments.blocked_by)
    blast_radius_count = 1
    payload = _payload(arguments)

    # 1.
    args_hash = policy.arguments_hash(payload)

    # 1a.
    replayed = idempotency.replay_completed(
        ctx.run_id, ctx.tool_call_id, tool_name, args_hash, actor_id=ctx.actor_id
    )
    if replayed is not None:
        return replayed

    # 0.
    requirement = policy.classify(tool_name, payload, blast_radius_count)
    if requirement.required and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"reason": requirement.reason})

    # 2.
    approval_row = runs.load_approval(ctx.run_id, ctx.tool_call_id)
    try:
        policy.check(
            ctx.actor_id,
            tool_name,
            payload,
            target_ids,
            approval_row,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            blast_radius_count=blast_radius_count,
        )
    except OutOfScopeError:
        replayed = idempotency.replay_completed(
            ctx.run_id,
            ctx.tool_call_id,
            tool_name,
            args_hash,
            actor_id=ctx.actor_id,
        )
        if replayed is not None:
            return replayed
        raise

    # 3.
    outcome = idempotency.acquire(ctx.run_id, ctx.tool_call_id, tool_name, args_hash)
    if outcome.action is LeaseAction.REPLAY:
        return outcome.result

    # 4.
    committed = False
    try:
        with _pool().connection() as conn:
            mutation = domain.update_task(ctx.actor_id, arguments, conn=conn)
            domain.write_events(
                ctx.run_id, ctx.actor_id, mutation.events, conn=conn
            )
            result = [task.model_dump(mode="json") for task in mutation.tasks]
            idempotency.complete(ctx.run_id, ctx.tool_call_id, result, conn=conn)
            conn.commit()
            committed = True
    except Exception as exc:
        if not committed:
            idempotency.fail(ctx.run_id, ctx.tool_call_id, str(exc))
        raise

    # 5.
    return result


def bulk_update_tasks(
    ctx: ToolContext,
    arguments: BulkUpdateTasksArgs,
) -> list[domain.TaskSnapshot]:
    """Update a caller-selected task set through the conditional approval gate."""
    tool_name = ToolName.BULK_UPDATE_TASKS.value
    target_ids = list(arguments.task_ids)
    if "blocked_by" in arguments.model_fields_set and arguments.blocked_by is not None:
        target_ids.append(arguments.blocked_by)
    blast_radius_count = len(arguments.task_ids)
    payload = _payload(arguments)

    # 1.
    args_hash = policy.arguments_hash(payload)

    # 1a.
    replayed = idempotency.replay_completed(
        ctx.run_id, ctx.tool_call_id, tool_name, args_hash, actor_id=ctx.actor_id
    )
    if replayed is not None:
        return replayed

    # 1b. D-50. Scope resolves before the raise, never after. Without this a
    #     call carrying four foreign or nonexistent ids classifies as over the
    #     blast radius and defers, so the caller receives ApprovalRequired where
    #     the contract requires OUT_OF_SCOPE, and the question of another actor's
    #     rows travels on to the component that builds the approval preview. The
    #     race recheck mirrors step 2: a preflight that lost to a committing
    #     caller sees a missing target, and a committed result still replays.
    try:
        policy.resolve_scope(ctx.actor_id, target_ids)
    except OutOfScopeError:
        replayed = idempotency.replay_completed(
            ctx.run_id,
            ctx.tool_call_id,
            tool_name,
            args_hash,
            actor_id=ctx.actor_id,
        )
        if replayed is not None:
            return replayed
        raise

    # 0. This is the conditional framework gate established by D-12. Hashing
    #    and completed replay are read only; lease acquisition remains after it.
    requirement = policy.classify(tool_name, payload, blast_radius_count)
    if requirement.required and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"reason": requirement.reason})

    # 2.
    approval_row = runs.load_approval(ctx.run_id, ctx.tool_call_id)
    try:
        policy.check(
            ctx.actor_id,
            tool_name,
            payload,
            target_ids,
            approval_row,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            blast_radius_count=blast_radius_count,
        )
    except OutOfScopeError:
        replayed = idempotency.replay_completed(
            ctx.run_id,
            ctx.tool_call_id,
            tool_name,
            args_hash,
            actor_id=ctx.actor_id,
        )
        if replayed is not None:
            return replayed
        raise

    # 3.
    outcome = idempotency.acquire(ctx.run_id, ctx.tool_call_id, tool_name, args_hash)
    if outcome.action is LeaseAction.REPLAY:
        return outcome.result

    # 4.
    committed = False
    try:
        with _pool().connection() as conn:
            mutation = domain.bulk_update_tasks(ctx.actor_id, arguments, conn=conn)
            domain.write_events(
                ctx.run_id, ctx.actor_id, mutation.events, conn=conn
            )
            result = [task.model_dump(mode="json") for task in mutation.tasks]
            idempotency.complete(ctx.run_id, ctx.tool_call_id, result, conn=conn)
            conn.commit()
            committed = True
    except Exception as exc:
        if not committed:
            idempotency.fail(ctx.run_id, ctx.tool_call_id, str(exc))
        raise

    # 5.
    return result


def delete_tasks(
    ctx: ToolContext,
    arguments: DeleteTasksArgs,
) -> list[domain.TaskSnapshot]:
    """Delete owned tasks only after the stored approval passes policy."""
    tool_name = ToolName.DELETE_TASKS.value
    target_ids = list(arguments.task_ids)
    blast_radius_count = len(arguments.task_ids)
    payload = _payload(arguments)

    # 1.
    args_hash = policy.arguments_hash(payload)

    # 1a.
    replayed = idempotency.replay_completed(
        ctx.run_id, ctx.tool_call_id, tool_name, args_hash, actor_id=ctx.actor_id
    )
    if replayed is not None:
        return replayed

    # 1b. D-50, same ordering as bulk_update_tasks and for the same reason. This
    #     tool is classified destructive at any count, so on the direct-call
    #     surface that section 12 requires, an unapproved call naming another
    #     actor's task reached the raise before any scope load. The framework
    #     route gates before the body and never showed it; the T10 gate passed
    #     approved=True for this tool and never showed it either.
    try:
        policy.resolve_scope(ctx.actor_id, target_ids)
    except OutOfScopeError:
        replayed = idempotency.replay_completed(
            ctx.run_id,
            ctx.tool_call_id,
            tool_name,
            args_hash,
            actor_id=ctx.actor_id,
        )
        if replayed is not None:
            return replayed
        raise

    # 0. The framework normally gates this tool before the body runs. Keeping
    #    the same guard here makes direct invocation fail closed too.
    requirement = policy.classify(tool_name, payload, blast_radius_count)
    if requirement.required and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"reason": requirement.reason})

    # 2.
    approval_row = runs.load_approval(ctx.run_id, ctx.tool_call_id)
    try:
        policy.check(
            ctx.actor_id,
            tool_name,
            payload,
            target_ids,
            approval_row,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            blast_radius_count=blast_radius_count,
        )
    except OutOfScopeError:
        replayed = idempotency.replay_completed(
            ctx.run_id,
            ctx.tool_call_id,
            tool_name,
            args_hash,
            actor_id=ctx.actor_id,
        )
        if replayed is not None:
            return replayed
        raise

    # 3.
    outcome = idempotency.acquire(ctx.run_id, ctx.tool_call_id, tool_name, args_hash)
    if outcome.action is LeaseAction.REPLAY:
        return outcome.result

    # 4.
    committed = False
    try:
        with _pool().connection() as conn:
            mutation = domain.delete_tasks(ctx.actor_id, arguments, conn=conn)
            domain.write_events(
                ctx.run_id, ctx.actor_id, mutation.events, conn=conn
            )
            result = [task.model_dump(mode="json") for task in mutation.tasks]
            idempotency.complete(ctx.run_id, ctx.tool_call_id, result, conn=conn)
            conn.commit()
            committed = True
    except Exception as exc:
        if not committed:
            idempotency.fail(ctx.run_id, ctx.tool_call_id, str(exc))
        raise

    # 5.
    return result


def propose_plan(
    ctx: ToolContext,
    arguments: ProposePlanArgs,
) -> list[dict]:
    """Return a display-only plan while tracking the invocation in its lease."""
    tool_name = ToolName.PROPOSE_PLAN.value
    target_ids: list[UUID] = []
    blast_radius_count = 0
    payload = _payload(arguments)

    # 1.
    args_hash = policy.arguments_hash(payload)

    # 1a.
    replayed = idempotency.replay_completed(
        ctx.run_id, ctx.tool_call_id, tool_name, args_hash, actor_id=ctx.actor_id
    )
    if replayed is not None:
        return replayed

    # 0.
    requirement = policy.classify(tool_name, payload, blast_radius_count)
    if requirement.required and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={"reason": requirement.reason})

    # 2.
    approval_row = runs.load_approval(ctx.run_id, ctx.tool_call_id)
    try:
        policy.check(
            ctx.actor_id,
            tool_name,
            payload,
            target_ids,
            approval_row,
            run_id=ctx.run_id,
            tool_call_id=ctx.tool_call_id,
            blast_radius_count=blast_radius_count,
        )
    except OutOfScopeError:
        replayed = idempotency.replay_completed(
            ctx.run_id,
            ctx.tool_call_id,
            tool_name,
            args_hash,
            actor_id=ctx.actor_id,
        )
        if replayed is not None:
            return replayed
        raise

    # 3.
    outcome = idempotency.acquire(ctx.run_id, ctx.tool_call_id, tool_name, args_hash)
    if outcome.action is LeaseAction.REPLAY:
        return outcome.result

    # 4. A plan has no domain mutation and therefore no task events. Completing
    #    the lease in the same transaction still makes its stored result the
    #    authoritative replay value.
    committed = False
    try:
        with _pool().connection() as conn:
            result = [payload]
            domain.write_events(ctx.run_id, ctx.actor_id, (), conn=conn)
            idempotency.complete(ctx.run_id, ctx.tool_call_id, result, conn=conn)
            conn.commit()
            committed = True
    except Exception as exc:
        if not committed:
            idempotency.fail(ctx.run_id, ctx.tool_call_id, str(exc))
        raise

    # 5.
    return result


def _payload(arguments) -> dict:
    """The one canonicalization rule, so every tool hashes the same way.

    The hash has to agree across three places that never see each other: the
    approval row T12B writes, the `arguments_hash` on the lease, and the
    comparison at policy step 5b. Two renderings of the same call would fail
    those comparisons for arguments that are in fact identical, so the rule
    cannot be per-tool taste.

    `exclude_unset` is not optional for the update-shaped models and is wrong for
    the others. `domain._update_parameters` reads `model_fields_set` to tell an
    omitted field from an explicit null, because `due_date=None` clears a due
    date while omitting `due_date` leaves it alone. A full dump marks every field
    as set, so hashing one would make those two different calls hash alike. The
    create-shaped models have no such distinction and dump completely.
    """
    if isinstance(arguments, MutableTaskFields):
        return arguments.model_dump(mode="json", exclude_unset=True)
    return arguments.model_dump(mode="json")


def _pool():
    """Imported lazily, matching `policy.py` and `idempotency.py`.

    `db.py` opens its `ConnectionPool` at import time, so a module-level import
    would make importing this file require a live database. Both kernel modules
    went out of their way to keep that property, and this file is the one that
    imports them to build every tool body, so importing eagerly here would undo
    it for the whole chain.
    """
    from .db import pool

    return pool
