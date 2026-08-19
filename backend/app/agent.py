"""The real Pydantic AI agent and the production AG-UI transport, from T12A.

Gate A already proved the transport and interrupt shapes on Day 1. This module
does not re-answer that question. It wires the proven shape to the real agent,
the registered Trellis tools, the real prompt boundary, the real `agent_runs` record, and
the real server-owned history, without letting the browser become an authority
for any of them.

**The whole trust boundary is one function, `_accepted_run_input`.** An AG-UI
client sends its entire transcript on every request, and `UIAdapter` appends
whatever `self.messages` yields to the `message_history` a caller supplies. So
filtering after the adapter is built would be filtering downstream of the thing
that reads the payload. Instead the incoming `RunAgentInput` is discarded and a
new one is constructed from scratch. An ordinary turn accepts the newest user
message plus one optional D-67 continuity locator used only to resolve
server-owned prior history. The locator itself never becomes model context.
Every field on the rebuilt adapter input remains server-chosen or empty.

That is a property a reader can check by grep rather than by reasoning about a
resolver's branches, which matters because reintroducing client-owned history is
the single most likely way a later change quietly breaks this build:

- `messages`         one accepted user message, nothing else
- `tools`            empty, so no client-declared frontend toolset is registered
- `state`            null, so no client state reaches `deps`
- `context`          empty
- `forwarded_props`  empty after D-67 locator extraction
- `resume`           absent, so `AGUIAdapter.deferred_tool_results` is None and
                     a client-asserted approval cannot continue a deferred call.
                     The approval bridge in BUILD_SPEC section 10 requires the
                     server to construct the resume result from its own stored
                     decision, and T12B does that below.
- `thread_id`        the server-issued `agent_runs.id`
- `run_id`           a fresh framework invocation id

**T12B narrows one word of that, and D-58 records the narrowing rather than
letting it happen inside a docstring edit.** A continuation request reads
`resume[].interruptId`, so the initial turn's "reads nothing at all" is no longer
true of every request on this route. What is true, and what the code below is
arranged to keep greppable:

```text
initial turn    client identity, history, and resume are read for nothing
continuation    resume[].interruptId is accepted as a lookup key only
                resume[].payload.approved is read for nothing
                the persisted approvals row decides ToolApproved or ToolDenied
```

`interruptId` grants nothing in exactly the sense `{id}` grants nothing on
`GET /api/runs/{id}`: it selects a server-owned record, and the record already
carries a decision this server persisted and verified. The `DeferredToolResults`
handed to the agent is built from that row and passed explicitly, so
`AGUIAdapter.deferred_tool_results`, which derives from the request payload,
stays unread on both paths.

Application run identity is server-owned. On an initial AG-UI user turn the
server creates the `agent_runs` row from the accepted newest user message and
uses that id as the application `run_id`. Client `threadId` and `runId` are not
resolved, validated, or consulted when creating application authority. They are
read for nothing at all. See the T12A block in `docs/DECISIONS.md`, which also
records the section 9 wording this narrows.

`agent_runs.prompt` and the message handed to the model therefore originate from
the same accepted value by construction. A two-call handshake that minted the
run from one request and ran the agent from another would let those two diverge,
so that a Run Inspector could display a benign prompt beside a hostile one.

Canonical history comes from `runs.load_history`, which BUILD_SPEC section 9
names as the only source of history anywhere in the codebase. Nothing here
constructs a message list from a request.
"""

import json
from dataclasses import dataclass, field
from functools import cache
from uuid import UUID, uuid4

from ag_ui.core import RunAgentInput, UserMessage
from fastapi import Request
from fastapi.responses import Response
from openai import AsyncOpenAI
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolApproved,
    ToolDenied,
)
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.ui.ag_ui import AGUIAdapter
from starlette.concurrency import run_in_threadpool

from . import domain, policy, prompts, runs, tools
from .config import settings
from .errors import OutOfScopeError, ValidationFailedError
from .models import (
    Approval,
    ApprovalPreview,
    ApprovalState,
    BulkUpdateTasksArgs,
    CreateTaskArgs,
    DeleteTasksArgs,
    GetTaskHistoryArgs,
    ListTasksArgs,
    ProposePlanArgs,
    ResolveTaskReferenceArgs,
    RunStatus,
    Task,
    ToolName,
    UpdateTaskArgs,
)


# Gate A fixed the interrupt identifier as `int-<tool_call_id>`, and API fact 6
# confirmed the framework maps a continuation back to the original call through
# it. Stripped in exactly one place, `_continuation_interrupt_id`.
_INTERRUPT_PREFIX = "int-"

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"

# T19. Retry only the failed provider HTTP request. Never replay the
# whole agent turn or an already-committed tool mutation.
_MODEL_REQUEST_MAX_RETRIES = 2

# The only two tools whose classification can require approval, and therefore the
# only two that can produce a deferred approval request. `delete_tasks` is gated
# declaratively; `bulk_update_tasks` raises `ApprovalRequired` from its own body
# under D-12. Anything else arriving as an approval request is refused rather
# than accommodated.
_APPROVAL_ARGS_MODELS = {
    ToolName.DELETE_TASKS.value: DeleteTasksArgs,
    ToolName.BULK_UPDATE_TASKS.value: BulkUpdateTasksArgs,
}

# The full product profile. Every existing caller gets this and nothing changes
# for the browser transport.
ALL_TOOLS = frozenset(name.value for name in ToolName)

# T00W. The profile a Linear AgentSession runs under, and it is deliberately
# narrower than the full browser profile.
#
# The two omitted tools are exactly the two that can require approval, and that
# is the whole argument. Trellis decides destructive work through a human
# pressing Approve on a card the server wrote, and the AG-UI transport carries
# that decision back as Pydantic AI deferred-tool continuation data. Linear has
# no such channel: an `elicitation` of type `select` returns an ordinary user
# `prompt` activity, which is a new message and not a resumption of an
# interrupted invocation. Wiring a Linear select into an approval row would
# produce a card no Linear answer can decide, which is worse than not offering
# the capability, because it looks like an approval boundary while being a dead
# end.
#
# So the boundary is enforced by absence. `delete_tasks` and
# `bulk_update_tasks` are not registered on the Linear agent at all: the model
# never sees them in the schema, cannot name them, and cannot reach their bodies
# by any prompt. Native Linear approval continuation is T16-adjacent work that
# T00W does not attempt, and this is what it looks like to not attempt it
# honestly.
LINEAR_TOOLS = frozenset(
    {
        ToolName.LIST_TASKS.value,
        ToolName.GET_TASK_HISTORY.value,
        ToolName.RESOLVE_TASK_REFERENCE.value,
        ToolName.CREATE_TASK.value,
        ToolName.UPDATE_TASK.value,
        ToolName.PROPOSE_PLAN.value,
    }
)


@dataclass(slots=True)
class RunEffects:
    mutation_committed: bool = False


@dataclass(frozen=True, slots=True)
class TrellisDeps:
    """The application values every tool body needs, carried on `RunContext`.

    `run_id` is the **application** run, `agent_runs.id`. `RunContext` also
    carries a `run_id`, and it is the other one: the framework's per-invocation
    identity, which changes when an approval interrupt splits an application run
    into two invocations. `tools.ToolContext` documents the same separation from
    the other side, and `_tool_context` below is the single place the two
    vocabularies meet.
    """

    actor_id: UUID
    run_id: UUID
    effects: RunEffects = field(default_factory=RunEffects)


def _runtime_model() -> Model:
    """Construct the configured model against NVIDIA hosted inference."""
    if not settings.nvidia_api_key:
        raise RuntimeError(
            "NVIDIA_API_KEY is required to construct the production runtime model"
        )

    client = AsyncOpenAI(
        base_url=NVIDIA_BASE_URL,
        api_key=settings.nvidia_api_key,
        timeout=settings.model_timeout_seconds,
        max_retries=_MODEL_REQUEST_MAX_RETRIES,
    )
    provider = OpenAIProvider(openai_client=client)
    return OpenAIChatModel(settings.model_id, provider=provider)


def build_agent(
    model: Model | str | None = None,
    *,
    toolset: frozenset[str] = ALL_TOOLS,
) -> Agent[TrellisDeps]:
    """Construct the agent from production or injected model state.

    NVIDIA hosted inference is the sole production provider. `_runtime_model`
    owns its OpenAI-compatible endpoint and credential, while `MODEL_ID`
    remains the only model selector and the identity stored with each run.

    The `model` parameter is an injection point, not a selector. `MODEL_ID`
    and `NVIDIA_API_KEY` are not read through the production construction path
    when a model is supplied. The parameter lets deterministic gates drive this
    identical toolset and prompt against a `FunctionModel` without credentials
    or a provider call.

    **`toolset` is a capability boundary, not a convenience.** A tool that is
    not registered does not exist for that agent: the model cannot see it in the
    schema, cannot name it, and cannot reach its body by any prompt. That is a
    stronger guarantee than refusing the call after the fact, because it removes
    the call rather than judging it, and it is why `LINEAR_TOOLS` is enforced
    here rather than by a check inside the worker.

    Defaulting to `ALL_TOOLS` keeps every existing caller, `get_agent` included,
    on exactly the profile it had before this parameter existed.
    """
    unknown = toolset - ALL_TOOLS
    if unknown:
        # A misspelled name would otherwise silently narrow the profile, and a
        # profile narrower than intended fails as a missing capability much
        # later and somewhere else.
        raise ValueError(f"unknown tools in toolset: {sorted(unknown)}")

    runtime_model = _runtime_model() if model is None else model
    agent = Agent(
        runtime_model,
        deps_type=TrellisDeps,
        instructions=prompts.SYSTEM_PROMPT,
        model_settings={"timeout": settings.model_timeout_seconds},
        retries={"tools": settings.max_tool_retries},
        tool_timeout=settings.tool_timeout_seconds,
        max_concurrency=1,
        # `DeferredToolRequests` is what lets an approval-required call surface
        # as an AG-UI interrupt rather than raise. It is transport shape, proven
        # at Gate A. It is not the approval bridge: T12A writes no approval row
        # and honours no client decision. See the limitations in
        # IMPLEMENTATION_NOTES.md.
        output_type=[str, DeferredToolRequests],
    )

    # The registered Trellis tools. Each wrapper does exactly one thing:
    # translate `RunContext` into `ToolContext` and call its deterministic tool
    # body. No policy, idempotency, or domain decision is made here. The
    # model chooses which typed tool to request; deterministic code still decides
    # what is allowed and what commits.
    #
    # A single Pydantic model parameter is flattened by Pydantic AI into the
    # tool's JSON schema, so the model sees section 10's explicit fields and
    # enums rather than a nested wrapper object.

    def _tool(name: str, **kwargs):
        """Register a tool only when the profile includes it.

        A tool outside `toolset` is discarded rather than registered and later
        refused, so it is absent from the schema the model is shown. See the
        `LINEAR_TOOLS` note above for why absence is the boundary.
        """
        if name not in toolset:
            return lambda func: func
        return agent.tool(**kwargs)

    @_tool(ToolName.LIST_TASKS.value)
    def list_tasks(
        ctx: RunContext[TrellisDeps], arguments: ListTasksArgs
    ) -> list[domain.TaskSnapshot]:
        """Read the user's tasks with typed status, date, priority, and limit filters."""
        return tools.list_tasks(_tool_context(ctx), arguments)

    @_tool(ToolName.GET_TASK_HISTORY.value)
    def get_task_history(
        ctx: RunContext[TrellisDeps],
        arguments: GetTaskHistoryArgs,
    ) -> dict:
        """Read one authoritative page of durable history for a task."""
        return tools.get_task_history(_tool_context(ctx), arguments)

    @_tool(ToolName.RESOLVE_TASK_REFERENCE.value)
    def resolve_task_reference(
        ctx: RunContext[TrellisDeps],
        arguments: ResolveTaskReferenceArgs,
    ) -> dict:
        """Resolve a current or historical task reference without mutation."""
        return tools.resolve_task_reference(_tool_context(ctx), arguments)

    @_tool(ToolName.CREATE_TASK.value)
    def create_task(
        ctx: RunContext[TrellisDeps], arguments: CreateTaskArgs
    ) -> list[domain.TaskSnapshot]:
        """Create one task with typed title, notes, due date, priority, and dependency fields."""
        result = tools.create_task(_tool_context(ctx), arguments)
        ctx.deps.effects.mutation_committed = True
        return result

    @_tool(ToolName.UPDATE_TASK.value)
    def update_task(
        ctx: RunContext[TrellisDeps], arguments: UpdateTaskArgs
    ) -> list[domain.TaskSnapshot]:
        """Update one task using its identifier and expected version."""
        result = tools.update_task(_tool_context(ctx), arguments)
        ctx.deps.effects.mutation_committed = True
        return result

    @_tool(ToolName.BULK_UPDATE_TASKS.value)
    def bulk_update_tasks(
        ctx: RunContext[TrellisDeps], arguments: BulkUpdateTasksArgs
    ) -> list[domain.TaskSnapshot]:
        """Apply the same typed changes to a list of task identifiers."""
        result = tools.bulk_update_tasks(_tool_context(ctx), arguments)
        ctx.deps.effects.mutation_committed = True
        return result

    # The one declarative gate. API fact 2 established `requires_approval=True`,
    # and fact 4 established that it accepts only a boolean, which is why
    # `bulk_update_tasks` raises from inside its own body instead. The framework
    # defers this call before the body runs at all, so `delete_tasks` never
    # reaches its own D-12 step 0 on this route.
    @_tool(ToolName.DELETE_TASKS.value, requires_approval=True)
    def delete_tasks(
        ctx: RunContext[TrellisDeps], arguments: DeleteTasksArgs
    ) -> list[domain.TaskSnapshot]:
        """Delete a list of tasks through the required approval path."""
        result = tools.delete_tasks(_tool_context(ctx), arguments)
        ctx.deps.effects.mutation_committed = True
        return result

    @_tool(ToolName.PROPOSE_PLAN.value)
    def propose_plan(
        ctx: RunContext[TrellisDeps], arguments: ProposePlanArgs
    ) -> list[dict]:
        """Return a summary and ordered steps for display without changing task state."""
        return tools.propose_plan(_tool_context(ctx), arguments)

    return agent


@cache
def get_agent() -> Agent[TrellisDeps]:
    """The process-wide agent, built once on first use.

    Deliberately lazy. `db.py` opens its pool at import time and `policy.py`,
    `idempotency.py`, and `tools.py` all went out of their way to keep importing
    a module from requiring a live service. Production construction requires
    `NVIDIA_API_KEY`, so constructing it at import would make importing
    `app.main` require a provider credential, and every deterministic test
    imports `app.main`.

    Callers reach this through the module attribute rather than a `from` import,
    so a test can substitute a deterministic model without a dependency-injection
    seam in the route.
    """
    return build_agent()


@cache
def get_linear_agent() -> Agent[TrellisDeps]:
    """The process-wide Linear AgentSession agent. T00W.

    Same model, same prompt, same tool bodies, same kernel. The one difference
    is the capability profile, and it is the difference that matters: a Linear
    session cannot reach `delete_tasks` or `bulk_update_tasks` because they are
    not registered on this agent. See `LINEAR_TOOLS`.

    Separate from `get_agent` rather than parameterized at the call site, so
    that "which profile does Linear run under" is answered by one cached
    function a reader can grep for rather than by an argument any caller could
    pass differently.
    """
    return build_agent(toolset=LINEAR_TOOLS)


async def handle_agui_request(request: Request) -> Response:
    """`POST /api/agui`. The production AG-UI transport.

    The order below is the contract, and each step is doing one job:

    1. Parse the AG-UI payload into `RunAgentInput`.
    2. Take the newest user message and optional D-67 continuity locator.
    3. Resolve server-owned continuity and open a fresh application run.
    4. Rebuild the run input from scratch so the adapter can read nothing else.
    5. Load canonical history through the single function section 9 names.
    6. Stream, recording failure, completion, history, and usage server-side.
    """
    run_input = AGUIAdapter.build_run_input(await request.body())

    # T12B. A payload carrying `resume` is a continuation of an approval this
    # server already decided, not a new turn. The branch is on the presence of
    # the field alone; nothing inside it is trusted, and the initial path below
    # is unchanged.
    interrupt_id = _continuation_interrupt_id(run_input)
    if interrupt_id is not None:
        return await _handle_continuation(request, interrupt_id)

    continuity_run_id = _accepted_continuity_run_id(run_input)
    user_message = _accepted_user_message(run_input)

    # D-67. The optional client continuity value is only a lookup key.
    # `create_turn` resolves server-owned state and creates a fresh
    # application run whose starting canonical history is already durable.
    run = await run_in_threadpool(
        runs.create_turn,
        settings.actor_id,
        user_message,
        settings.model_id,
        continuity_run_id,
    )
    adapter: AGUIAdapter[TrellisDeps, str | DeferredToolRequests] = AGUIAdapter(
        get_agent(),
        _accepted_run_input(run.id, user_message),
        accept=request.headers.get("accept"),
    )

    # Step 5. A root run starts empty; a D-67 successor is born with its
    # inherited canonical snapshot already persisted. Either way the model gets
    # history only through this database read, never from the submitted
    # transcript. `test_agui_forged_history_ignored` protects that boundary.
    history = await run_in_threadpool(runs.load_history, run.id, settings.actor_id)
    message_history = ModelMessagesTypeAdapter.validate_json(json.dumps(history))

    deps = TrellisDeps(actor_id=settings.actor_id, run_id=run.id)
    native = adapter.run_stream_native(
        message_history=message_history,
        deps=deps,
    )
    events = adapter.transform_stream(
        _record_failure(native, run.id, deps.effects),
        on_complete=_completion_recorder(run.id),
    )
    return adapter.streaming_response(events)


def _continuation_interrupt_id(run_input: RunAgentInput) -> str | None:
    """The one value a continuation payload contributes, or None for a new turn.

    Returns the `tool_call_id` carried by `resume[].interruptId`, which Gate A
    fixed as `int-<tool_call_id>` and API fact 6 confirmed survives the
    continuation. The prefix is stripped here and nowhere else.

    More than one entry is refused rather than iterated. D-56 permits at most one
    simultaneously pending approval per application run, so a payload offering
    two continuations is describing a state this server cannot have produced, and
    answering the first would be the first-row-wins behaviour D-45 rejects.

    `status` and `payload` are not read. `payload.approved` in particular is the
    browser's claim about what the human chose, and the server already has its
    own record of that.
    """
    resume = getattr(run_input, "resume", None)
    if not resume:
        return None
    if len(resume) != 1:
        raise ValidationFailedError(
            "a continuation carries exactly one interrupt; see D-56"
        )
    interrupt_id = resume[0].interrupt_id or ""
    tool_call_id = interrupt_id.removeprefix(_INTERRUPT_PREFIX)
    if not tool_call_id:
        raise ValidationFailedError("continuation carries no interrupt id")
    return tool_call_id


async def _handle_continuation(request: Request, tool_call_id: str) -> Response:
    """Continue one application run from the decision this server persisted.

    The order is the contract, and it is the same shape as the initial turn: the
    payload names a record, the server resolves it, and everything the model sees
    afterwards is server owned.

    1. Resolve the call id to its approval row, actor scoped. Zero eligible rows
       and more than one both refuse, per D-51.
    2. Build the framework result from the stored decision, never from the
       payload.
    3. Load canonical history for the application run the row names.
    4. Stream a fresh framework invocation under that same application run.

    Step 4 is why BUILD_SPEC proof 5 is worded the way it is. This is one more
    invocation inside one `agent_runs` record, not a resumption of the previous
    one, and usage accumulates across both because `record_usage` adds.
    """
    approval = await run_in_threadpool(
        runs.resolve_continuation, tool_call_id, settings.actor_id
    )
    run_id = approval.run_id

    adapter: AGUIAdapter[TrellisDeps, str | DeferredToolRequests] = AGUIAdapter(
        get_agent(),
        _continuation_run_input(run_id),
        accept=request.headers.get("accept"),
    )

    history = await run_in_threadpool(runs.load_history, run_id, settings.actor_id)
    message_history = ModelMessagesTypeAdapter.validate_json(json.dumps(history))

    deps = TrellisDeps(actor_id=settings.actor_id, run_id=run_id)
    native = adapter.run_stream_native(
        message_history=message_history,
        deferred_tool_results=_deferred_results(approval),
        deps=deps,
    )
    events = adapter.transform_stream(
        _record_failure(native, run_id, deps.effects),
        on_complete=_completion_recorder(run_id),
    )
    return adapter.streaming_response(events)


def _deferred_results(approval: Approval) -> DeferredToolResults:
    """The stored decision, as the framework's continuation result.

    This function is the entire answer to "who decides whether the deletion
    happens". It reads one column of one server-owned row. It takes no argument
    derived from the request, which is what makes the client's `payload.approved`
    unable to influence the outcome in either direction.

    `ToolApproved` is constructed with no `override_args`. The framework offers
    them, and accepting any would let the continuation execute arguments the
    approval row never covered, which is precisely what `arguments_hash` and
    policy step 5b exist to prevent.
    """
    approved = approval.decision is ApprovalState.APPROVED
    result = ToolApproved() if approved else ToolDenied()
    return DeferredToolResults(approvals={approval.tool_call_id: result})


def _continuation_run_input(run_id: UUID) -> RunAgentInput:
    """The payload a continuation invocation is allowed to see.

    Empty `messages`, because a continuation adds no user turn: the model is
    resuming from history plus a tool result. Every other field matches
    `_accepted_run_input` for the same reasons, and `resume` is absent here too,
    so the adapter derives no deferred results of its own from a request. The
    ones it uses are passed explicitly by `_handle_continuation`.
    """
    return RunAgentInput(
        thread_id=str(run_id),
        run_id=str(uuid4()),
        state=None,
        messages=[],
        tools=[],
        context=[],
        forwarded_props={},
    )


def _accepted_continuity_run_id(
    run_input: RunAgentInput,
) -> UUID | None:
    """Extract D-67's optional continuity lookup key.

    `forwardedProps` remains untrusted transport. Exactly one field may
    nominate a previously server-issued application run. Ownership,
    existence, eligibility, and history remain server-side decisions.

    The forwarded properties themselves never reach the adapter/model:
    `_accepted_run_input` reconstructs `forwarded_props={}`.
    """
    forwarded = run_input.forwarded_props or {}
    key = "trellisContinuityRunId"

    if key not in forwarded:
        return None

    value = forwarded[key]

    if not isinstance(value, str):
        raise OutOfScopeError()

    try:
        return UUID(value)
    except ValueError:
        raise OutOfScopeError() from None


def _accepted_user_message(run_input: RunAgentInput) -> str:
    """The newest user message, and nothing else, out of the whole payload.

    Everything before it is prior client transcript and carries no authority. A
    payload with no user message is a 422 rather than an empty prompt, because
    `agent_runs.prompt` is `NOT NULL` and a run whose prompt of record is blank
    would be an audit row that explains nothing.
    """
    for message in reversed(run_input.messages):
        if not isinstance(message, UserMessage):
            continue
        text = _message_text(message.content)
        if text:
            return text
    raise ValidationFailedError("AG-UI payload carries no user message")


def _message_text(content: object) -> str:
    """AG-UI user content is either a string or a list of typed input parts."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            item.text
            for item in content
            if isinstance(getattr(item, "text", None), str)
        ]
        return "\n".join(parts).strip()
    return ""


def _accepted_run_input(run_id: UUID, user_message: str) -> RunAgentInput:
    """The payload the adapter is allowed to see. See this module's docstring.

    `thread_id` carries the server-issued `agent_runs.id` outward. The adapter
    echoes it on `RUN_STARTED` and `RUN_FINISHED`, so the browser learns the
    application run id over the protocol it is already speaking, which is what
    T12B's `POST /api/runs/{id}/approvals/{tool_call_id}` and T20's Run Inspector
    need. It travels outward only. A value arriving in this field on a later
    request is still read for nothing.
    """
    return RunAgentInput(
        thread_id=str(run_id),
        run_id=str(uuid4()),
        state=None,
        messages=[UserMessage(id=str(uuid4()), role="user", content=user_message)],
        tools=[],
        context=[],
        forwarded_props={},
    )


def _open_approval(run_id: UUID, requests: DeferredToolRequests) -> None:
    """Turn one deferred approval request into the authoritative pending row.

    BUILD_SPEC section 10's bridge requires this to happen before application
    state claims an approval is pending, and D-06 requires the row rather than
    the interrupt to be the authorization record. The AG-UI interrupt is a UI
    event; this is the thing `policy.check` will verify against later.

    Every refusal below raises, and the caller marks the run failed. Failing
    closed is the only safe direction here: the framework has already stopped the
    tool body, so refusing to write a row means nothing executes.

    **The preview scope guard is the reason this function is careful about
    order.** `delete_tasks` is gated declaratively, so the framework defers it
    before its body runs, which means neither the tool's own
    `policy.resolve_scope` nor the authoritative `policy.check` has executed when
    the card is built. `resolve_scope` therefore runs here, before any task
    detail is fetched. Fetching first and validating afterwards would put another
    actor's titles on screen and then discover the problem, and a disclosure that
    already happened is not undone by refusing the mutation.

    This does not replace the tool body's own check, and D-50 says so directly.
    Ownership can move between the preview, the human decision, and the
    continuation, so `policy.check` resolves scope again on the approved path.
    """
    # `calls` is for tools the client executes, which this build does not use.
    # A non-empty list means the agent was configured with a frontend toolset,
    # and continuing would mean approving something whose execution the server
    # does not own.
    if requests.calls:
        raise ValidationFailedError("client-executed deferred calls are not supported")

    # D-56, fail closed. Zero is a framework contract violation; more than one is
    # the case D-45 left open, and the ruling is that no call is chosen, no row
    # is written, and the turn is refused whole.
    if len(requests.approvals) != 1:
        raise ValidationFailedError(
            f"{len(requests.approvals)} simultaneous approval requests; "
            "D-56 permits one per application run"
        )

    part = requests.approvals[0]
    args_model = _APPROVAL_ARGS_MODELS.get(part.tool_name)
    if args_model is None:
        raise ValidationFailedError(
            f"{part.tool_name} cannot require approval; see policy.classify"
        )
    arguments = args_model.model_validate(part.args_as_dict())

    # Mirrors the target and count derivation in the matching `tools.py` body
    # exactly. The scope guard has to cover the same id set the tool will act on,
    # and the count has to reach `classify` the same way, or the card could
    # describe a different call from the one the approval authorizes.
    target_ids = list(arguments.task_ids)
    if isinstance(arguments, BulkUpdateTasksArgs):
        if (
            "blocked_by" in arguments.model_fields_set
            and arguments.blocked_by is not None
        ):
            target_ids.append(arguments.blocked_by)

    # `tools._payload` is the single canonicalization rule, and its own docstring
    # names "the approval row T12B writes" as one of the three places the hash
    # has to agree. Recomputing it here with a second rendering is exactly the
    # drift it warns about, so this reuses the function rather than the idea.
    payload = tools._payload(arguments)
    args_hash = policy.arguments_hash(payload)

    # Scope first. Everything below this line reads task content.
    policy.resolve_scope(settings.actor_id, target_ids)

    requirement = policy.classify(part.tool_name, payload, len(arguments.task_ids))
    if not requirement.required:
        # The framework deferred a call policy does not consider gated. Writing a
        # row would record an approval requirement the policy layer disowns.
        raise ValidationFailedError(
            f"{part.tool_name} deferred without a policy requirement"
        )

    rows = runs.load_owned_tasks(settings.actor_id, list(arguments.task_ids))
    snapshots = [Task.model_validate(row).model_dump(mode="json") for row in rows]
    preview = (
        ApprovalPreview(deletes=snapshots)
        if isinstance(arguments, DeleteTasksArgs)
        else ApprovalPreview(updates=snapshots)
    )

    runs.open_approval(
        run_id,
        tool_call_id=part.tool_call_id,
        tool_name=part.tool_name,
        arguments=payload,
        arguments_hash=args_hash,
        required_reason=requirement.reason.value,
        preview=preview.model_dump(mode="json"),
    )


def _tool_context(ctx: RunContext[TrellisDeps]) -> tools.ToolContext:
    """Framework context to application context. The only adaptation point.

    `tool_call_id` and `tool_call_approved` come from the framework because they
    describe this invocation. `actor_id` and the application `run_id` come from
    dependencies because they describe the application. `ctx.run_id` is
    deliberately not read: it is the framework invocation, and `tools.py` says in
    terms that nothing may take the application run from it.
    """
    return tools.ToolContext(
        actor_id=ctx.deps.actor_id,
        run_id=ctx.deps.run_id,
        tool_call_id=ctx.tool_call_id or "",
        tool_call_approved=ctx.tool_call_approved,
    )


async def _record_failure(stream, run_id: UUID, effects: RunEffects):
    """Persist failure while distinguishing pre-commit from post-commit failure."""
    try:
        async for event in stream:
            yield event
    except Exception as exc:
        if effects.mutation_committed:
            stored_error = f"mutation_committed=true; response_error={exc}"
            await run_in_threadpool(
                runs.set_status, run_id, RunStatus.FAILED, stored_error
            )
            raise RuntimeError(
                "At least one task change was committed, but the assistant response "
                "could not finish. The board will refresh from committed state."
            ) from exc

        await run_in_threadpool(
            runs.set_status, run_id, RunStatus.FAILED, str(exc)
        )
        raise

def _completion_recorder(run_id: UUID):
    """Persist server-owned history, usage, and terminal status on success."""

    async def record(result) -> None:
        # Fact 3 fixes the serialization: dump with ModelMessagesTypeAdapter and
        # store the JSON array in the jsonb column. This replaces rather than
        # appends, because `all_messages()` already contains the history this
        # invocation was given.
        messages = json.loads(
            ModelMessagesTypeAdapter.dump_json(result.all_messages())
        )
        await run_in_threadpool(runs.save_history, run_id, messages)

        # `record_usage` adds rather than replaces, because one application run
        # can contain several invocations. `cost_cents` stays at its default:
        # Pydantic AI reports `cost` as None without a pricing source, and an
        # invented number in an audit row is worse than a zero.
        usage = result.usage
        await run_in_threadpool(
            _record_usage,
            run_id,
            usage.requests,
            usage.tool_calls,
            usage.input_tokens,
            usage.output_tokens,
        )

        # A deferred output means the framework gated an approval-required call.
        # The run is not finished and must not be marked completed. T12B writes
        # the pending `approvals` row and moves the run to `awaiting_approval` in
        # one transaction, which is the approval bridge T12A deliberately left
        # unbuilt.
        #
        # A refusal here fails the run explicitly. `_record_failure` wraps the
        # native stream and cannot see this, because `on_complete` runs after the
        # agent finished successfully from the framework's point of view. Without
        # this the fail-closed paths in `_open_approval` would leave a run
        # `running` forever with no row and no error, which is the worst of the
        # three outcomes.
        if isinstance(result.output, DeferredToolRequests):
            try:
                await run_in_threadpool(_open_approval, run_id, result.output)
            except Exception as exc:
                await run_in_threadpool(
                    runs.set_status, run_id, RunStatus.FAILED, str(exc)
                )
                raise
            return
        await run_in_threadpool(runs.set_status, run_id, RunStatus.COMPLETED)

    return record


def _record_usage(
    run_id: UUID,
    model_calls: int,
    tool_calls: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Positional shim so `run_in_threadpool` can call a keyword-only function."""
    runs.record_usage(
        run_id,
        model_calls=model_calls,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
