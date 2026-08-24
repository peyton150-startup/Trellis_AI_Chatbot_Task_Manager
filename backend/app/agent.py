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

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cache
from uuid import UUID, uuid4

from ag_ui.core import (
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    UserMessage,
)
from fastapi import Request
from fastapi.responses import Response
from openai import AsyncOpenAI
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    ModelRetry,
    RunContext,
    ToolApproved,
    ToolDenied,
    ToolFailed,
)
from pydantic_core import to_jsonable_python
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.ui.ag_ui import AGUIAdapter, AGUIEventStream
from starlette.concurrency import run_in_threadpool

from . import domain, limits, policy, prompts, runs, tools
from .config import settings
from .errors import (
    AppendNotesLimitError,
    BulkTargetCoverageError,
    ExternalDivergenceError,
    OutOfScopeError,
    ValidationFailedError,
    VersionConflictError,
)
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
    UndoReason,
    UpdateTaskArgs,
)


# Gate A fixed the interrupt identifier as `int-<tool_call_id>`, and API fact 6
# confirmed the framework maps a continuation back to the original call through
# it. Stripped in exactly one place, `_continuation_interrupt_id`.
_INTERRUPT_PREFIX = "int-"

log = logging.getLogger("trellis.agent")

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


# ---------------------------------------------------------------------------
# D-76. Run-relative undo, as application control flow rather than a model tool.
#
# The browser sends two independent lookup keys, and conflating them is the bug
# this decision exists to prevent:
#
#     trellisContinuityRunId   the newest COMPLETED run, whose canonical history
#                              may seed the next turn. D-67 owns it.
#     trellisPreviousRunId     the newest server-issued application run of any
#                              status, and the only candidate target of an
#                              "undo that".
#
# They diverge exactly when a run committed a mutation and then failed, which is
# precisely the case a user is most likely to want undone. Continuity does not
# advance past a failed run, by design, so targeting continuity would silently
# undo an older successful action instead. See D-76.
#
# Both remain untrusted lookup keys. Neither is history, authorization, or proof
# the run exists, and the server resolves ownership, status, and eligibility for
# itself.
_PREVIOUS_RUN_KEY = "trellisPreviousRunId"
_CONTINUITY_RUN_KEY = "trellisContinuityRunId"

# The complete accepted grammar, matched whole after normalization. Membership in
# a frozen set rather than a pattern, because the failure modes are not
# symmetric: a phrase this misses costs the user one rephrase, while a phrase it
# wrongly claims silently compensates a run they did not name. "restore D75" and
# "bring back my old farm tasks" both require deciding *what* to restore, which
# is target interpretation D-76 does not authorize, so neither is here.
_UNDO_PREVIOUS_COMMANDS = frozenset(
    {
        "undo",
        "undo that",
        "undo it",
        "undo what you just did",
        "undo the last action",
        "revert that",
        "reverse that",
        "recover what you just deleted",
        "recover everything you just deleted",
        "restore what you just deleted",
        "restore everything you just deleted",
        "bring back what you just deleted",
        "bring back everything you just deleted",
        "bring back the tasks you just deleted",
    }
)

# Trailing punctuation only. Nothing here rewrites words, drops a leading
# "please", or strips an interior clause, because each of those widens what
# counts as the same command and the whole point of the set above is that it
# is narrow.
_COMMAND_PUNCTUATION = ".?!"

# What the synthetic control messages carry for a later reader. Observational
# only: nothing reads it back to make a decision, and it is not authorization,
# not the target id, and not derived from the browser.
_CONTROL_METADATA_KEY = "trellis_control"
_CONTROL_UNDO_PREVIOUS = "undo_previous"

# Missing, malformed, and foreign all produce this one sentence. A user who
# nominates another actor's run must not be able to tell it apart from a user
# who nominated nothing, which is the same indistinguishability every other
# resolver in this build maintains.
_NO_TARGET_TEXT = (
    "There is no previous Trellis action in this conversation that I can undo."
)

_UNDO_INELIGIBLE_TEXT = {
    runs.UndoIneligibility.STILL_ACTIVE: (
        "The previous action has not finished yet, so there is nothing settled "
        "to undo."
    ),
    runs.UndoIneligibility.NO_EFFECTS: (
        "The previous action did not change any tasks, so there is nothing to "
        "undo."
    ),
    runs.UndoIneligibility.ALREADY_COMPENSATED: (
        "The previous action has already been undone. Undo applies once and "
        "never redoes."
    ),
}

_UNDO_REFUSED_TEXT = {
    UndoReason.VERSION_CONFLICT: (
        "I did not undo anything. At least one of those tasks has changed since "
        "that action, so restoring it would overwrite the newer change."
    ),
    UndoReason.ROW_DISAPPEARED: (
        "I did not undo anything. At least one of those tasks no longer exists, "
        "so the change cannot be reversed as a whole."
    ),
    UndoReason.ROW_RECREATED: (
        "I did not undo anything. At least one of those tasks has been created "
        "again since that action, so restoring the original would collide with "
        "it."
    ),
    UndoReason.EXTERNALLY_MODIFIED: (
        "I did not undo anything. At least one of those tasks was changed "
        "outside Trellis, so reversing the action here could discard that "
        "change."
    ),
}


@dataclass(frozen=True, slots=True)
class _ControlOutcome:
    """What the deterministic control turn decided, before anything is emitted.

    `mutation_committed` mirrors `RunEffects` on the model path and is read for
    the same reason: if persisting the turn fails after compensation already
    committed, the stored error has to say so, because the board has moved and
    the browser must refetch rather than assume nothing happened.
    """

    text: str
    applied: int = 0
    mutation_committed: bool = False


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


def _nvidia_profile() -> OpenAIModelProfile:
    """The provider contract, named so tests can exercise it rather than copy it.

    D-81. This lives in its own function for a reason found by mutation testing:
    when it was built inline, the wire tests constructed an equivalent profile of
    their own, so removing either field from production left them green. They
    were proving that a profile configured this way produces that body, not that
    Trellis is configured this way.

    `openai_chat_thinking_field` names the field NVIDIA returns reasoning in, so
    the contract is stated rather than autodetected.

    `openai_chat_supports_max_completion_tokens=False` is the one that would
    otherwise fail silently. Pydantic AI serialises `ModelSettings.max_tokens` as
    `max_completion_tokens` unless the profile says the model does not support
    it, and NVIDIA's hosted Chat Completions documents `max_tokens`. Sent under
    the wrong name the cap is ignored, which looks exactly like a cap that was
    never set.

    `openai_chat_send_back_thinking_parts` is deliberately left at its default.
    It governs replaying reasoning inside one trajectory, which is the case
    NVIDIA wants preserved; the case it wants dropped is the previous user turn,
    and that is `_project_prior_turn_history_for_model`.
    """
    return OpenAIModelProfile(
        openai_chat_thinking_field="reasoning",
        openai_chat_supports_max_completion_tokens=False,
    )


def _model_settings() -> dict:
    """What every model request carries, instead of provider defaults.

    D-81. Trellis previously sent only a timeout, which left three of NVIDIA's
    defaults in charge of behaviour it cares about: `reasoning_budget` at 16384,
    `max_tokens` at 16384, and `temperature` at 1. The first two decide how long
    the model may think before it can act, and the third makes routing vary run
    to run for no benefit this application wants.

    `temperature=0` is for low-variance tool routing, not for generation
    quality. `top_p` is deliberately left alone: NVIDIA advises against tuning
    both in one request, and one knob is enough to reason about.

    `reasoning_budget` and `chat_template_kwargs` are NVIDIA-specific and travel
    through `extra_body`, which is the supported passthrough. Thinking is
    enabled explicitly rather than relied upon: this model reasons by default,
    but a default is not a contract.

    The budget is capped rather than switched off. The point is a bounded
    reasoning step, not a model that stops thinking.
    """
    return {
        "timeout": settings.model_timeout_seconds,
        "temperature": 0.0,
        "max_tokens": settings.model_max_tokens,
        "extra_body": {
            "reasoning_budget": settings.model_reasoning_budget,
            "chat_template_kwargs": {"enable_thinking": True},
        },
    }


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
    return OpenAIChatModel(
        settings.model_id, provider=provider, profile=_nvidia_profile()
    )


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
        model_settings=_model_settings(),
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
        """Read the user's current tasks with typed status, date, priority, and limit filters, optionally only tasks whose title is duplicated."""
        with _model_facing_refusals():
            return tools.list_tasks(_tool_context(ctx), arguments)

    @_tool(ToolName.GET_TASK_HISTORY.value)
    def get_task_history(
        ctx: RunContext[TrellisDeps],
        arguments: GetTaskHistoryArgs,
    ) -> dict:
        """Read one authoritative page of durable history for a task."""
        with _model_facing_refusals():
            return tools.get_task_history(_tool_context(ctx), arguments)

    @_tool(ToolName.RESOLVE_TASK_REFERENCE.value)
    def resolve_task_reference(
        ctx: RunContext[TrellisDeps],
        arguments: ResolveTaskReferenceArgs,
    ) -> dict:
        """Resolve a current or historical task reference without mutation."""
        with _model_facing_refusals():
            return tools.resolve_task_reference(_tool_context(ctx), arguments)

    @_tool(ToolName.CREATE_TASK.value)
    def create_task(
        ctx: RunContext[TrellisDeps], arguments: CreateTaskArgs
    ) -> list[domain.TaskSnapshot]:
        """Create one task with typed title, notes, due date, priority, and dependency fields."""
        with _model_facing_refusals():
            result = tools.create_task(_tool_context(ctx), arguments)
        ctx.deps.effects.mutation_committed = True
        return result

    @_tool(ToolName.UPDATE_TASK.value)
    def update_task(
        ctx: RunContext[TrellisDeps], arguments: UpdateTaskArgs
    ) -> list[domain.TaskSnapshot]:
        """Update one task using its identifier and expected version.

        Two ways to change notes, and they are not interchangeable:

            notes         replaces the whole note value. Send "" to clear.
            append_notes  contains ONLY the new text. The server reads the
                          current notes and appends to them.

        To add to a task's notes, send append_notes with just the new content.
        Do not read the existing notes in order to concatenate them yourself,
        and do not repeat existing notes inside append_notes. Sending both
        fields in one call is refused.
        """
        with _model_facing_refusals():
            result = tools.update_task(_tool_context(ctx), arguments)

        ctx.deps.effects.mutation_committed = True
        return result

    # `sequential=True` under D-79. `max_concurrency=1` above bounds concurrent
    # agent runs; it does not stop Pydantic AI from scheduling several tool
    # calls out of one model response concurrently, and Trellis tools are
    # synchronous, so those run on worker threads. This tool takes row locks on
    # a caller-chosen set and holds them for its whole transaction, so it is the
    # one that must not overlap a sibling call in the same response.
    #
    # The scope is exactly that and no wider. Separate HTTP requests, separate
    # `agent.run()` calls, and separate workers are unaffected: their
    # correctness is PostgreSQL's, through the canonical lock order and the
    # version predicates, and it stays PostgreSQL's.
    @_tool(ToolName.BULK_UPDATE_TASKS.value, sequential=True)
    def bulk_update_tasks(
        ctx: RunContext[TrellisDeps], arguments: BulkUpdateTasksArgs
    ) -> list[domain.TaskSnapshot]:
        """Apply the same typed changes to several tasks in one transaction.

        Prefer this over repeated update_task calls whenever two or more tasks
        receive the SAME patch: one call, every identifier in task_ids.

            "set A, B and C to high priority"  -> ONE bulk_update_tasks call
            "set A high and B low"             -> separate update_task calls

        Every named task gets the identical change, so a request that gives
        different values to different tasks is not a bulk update. There is no
        expected_version field: the server reads each task's current version
        under lock.

        Notes can be replaced or appended, and the two are separate modes:

            notes         replaces the whole note value on every named task.
            append_notes  contains ONLY the new text. The server reads each
                          task's current notes and appends to them, so different
                          tasks keep their different existing notes.

        To add the same line to several tasks, send ONE call with every id in
        task_ids and the new text in append_notes. Do not read the notes and
        concatenate them yourself, and do not send one update_task per task.
        append_notes cannot be combined with any other field in the same call;
        send the append by itself. Either the whole set is appended or none of
        it is.

        Two rules the schema cannot express, so read them here:

            Send at least one field you are changing. A call with task_ids and
            nothing else is refused.

            Omit fields you are not changing. Do NOT send them as null: a null
            title, notes, priority, or status is read as "leave it alone", so a
            call carrying only those is refused as changing nothing. Null is
            meaningful for exactly two fields, due_date and blocked_by, where it
            clears the value.
        """
        with _model_facing_refusals():
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
        with _model_facing_refusals():
            result = tools.delete_tasks(_tool_context(ctx), arguments)
        ctx.deps.effects.mutation_committed = True
        return result

    @_tool(ToolName.PROPOSE_PLAN.value)
    def propose_plan(
        ctx: RunContext[TrellisDeps], arguments: ProposePlanArgs
    ) -> list[dict]:
        """Return a summary and ordered steps for display without changing task state."""
        with _model_facing_refusals():
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
    previous_run_id = _accepted_previous_run_id(run_input)
    user_message = _accepted_user_message(run_input)

    # D-76. A recognized run-relative control command never reaches the model.
    # The branch sits after the approval continuation, because a resume payload
    # is a continuation of a decision and must never be reinterpreted as a new
    # command, and after message extraction, because the command is the message.
    #
    # Everything below this line is the unchanged D-67 model path.
    if _is_undo_previous_command(user_message):
        return await _handle_undo_previous(
            request, user_message, previous_run_id, continuity_run_id
        )

    # D-67. The optional client continuity value is only a lookup key.
    # `create_turn` resolves server-owned state and creates a fresh
    # application run whose starting canonical history is already durable.
    created = await run_in_threadpool(
        runs.create_turn,
        settings.actor_id,
        user_message,
        settings.model_id,
        continuity_run_id,
    )
    adapter: AGUIAdapter[TrellisDeps, str | DeferredToolRequests] = AGUIAdapter(
        get_agent(),
        _accepted_run_input(created.id, user_message),
        accept=request.headers.get("accept"),
    )

    # Step 5. A root run starts empty; a D-67 successor is born with its
    # inherited canonical snapshot already persisted. Either way the model gets
    # history only from server-owned state, never from the submitted
    # transcript. `test_agui_forged_history_ignored` protects that boundary.
    # The snapshot comes back from the committed write above, so there is no
    # second read to ask the database what it just stored.
    message_history = ModelMessagesTypeAdapter.validate_python(
        created.message_history
    )

    # D-81. A new ordinary user turn, so the predecessor's reasoning is dropped
    # from what the provider sees. The stored snapshot above keeps it; only this
    # projection differs. `_handle_continuation` deliberately does not do this,
    # because a continuation is still the same user turn.
    message_history = _project_prior_turn_history_for_model(message_history)

    deps = TrellisDeps(actor_id=settings.actor_id, run_id=created.id)
    native = adapter.run_stream_native(
        message_history=message_history,
        deps=deps,
    )
    events = adapter.transform_stream(
        _record_failure(native, created.id, deps.effects),
        on_complete=_completion_recorder(created.id),
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
    message_history = ModelMessagesTypeAdapter.validate_python(history)

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


def _is_undo_previous_command(user_message: str) -> bool:
    """Whether this message is an unambiguous "undo the last thing" command.

    Deliberately conservative, and deliberately not a classifier that returns a
    target. The only two answers are "this names the immediately previous
    application action" and "this does not", and everything else, including which
    run that is and which tasks it touched, is resolved from server-owned state
    afterwards. A message that names a task, a title, a date, or anything else
    requiring interpretation falls through to the model, where the ordinary
    tools, policy layer, and approval bridge apply as they always have.
    """
    normalized = " ".join(user_message.casefold().split())
    return normalized.rstrip(_COMMAND_PUNCTUATION).strip() in _UNDO_PREVIOUS_COMMANDS


async def _handle_undo_previous(
    request: Request,
    user_message: str,
    previous_run_id: UUID | None,
    continuity_run_id: UUID | None,
) -> Response:
    """D-76. Compensate the previous application run with no model in the loop.

    Zero provider requests, zero framework runs, zero tool calls. `get_agent` is
    not called and no `Agent` is constructed, which matters beyond cost: building
    one requires `NVIDIA_API_KEY`, and a deterministic command should not depend
    on a credential it has no use for.

    What this still produces is an ordinary Trellis turn. A real `agent_runs`
    row, server-owned canonical history the next model turn will inherit, and a
    normal AG-UI response. From the browser it is indistinguishable in shape from
    any other turn, which is the point: continuity, the Run Inspector, and the
    next conversation turn all keep working without learning about a special
    case.

    The two locators are resolved for different questions and never substitute
    for each other. `previous_run_id` names the undo target and nothing else.
    `continuity_run_id` supplies history and grants no undo authority. When the
    previous run is itself completed it may serve as both, so the control turn
    reads as following the action it reversed.
    """
    predecessor = await run_in_threadpool(
        _control_history_predecessor, previous_run_id, continuity_run_id
    )
    created = await run_in_threadpool(
        runs.create_control_turn,
        settings.actor_id,
        user_message,
        predecessor,
    )

    stream: AGUIEventStream[TrellisDeps, str] = AGUIEventStream(
        run_input=_control_run_input(created.id),
        accept=request.headers.get("accept"),
    )
    return stream.streaming_response(
        _control_events(stream.run_input, created, user_message, previous_run_id)
    )


async def _control_events(
    run_input: RunAgentInput,
    created: runs.CreatedTurn,
    user_message: str,
    previous_run_id: UUID | None,
):
    """The AG-UI events of one control turn, in the one order that is truthful.

    `RUN_STARTED` goes first because the browser learns the application run id
    from it, and both `useRun` and the D-76 previous-run locator read it there.

    `RUN_FINISHED` goes last, after the run is durably `completed`. That is not
    cosmetic ordering. The browser reacts to run end by fetching
    `GET /api/runs/{id}` and advances continuity only on `completed`, so a
    `RUN_FINISHED` emitted while the row still said `running` would lose the
    turn from the conversation for no reason other than a race this code chose
    to create.

    Everything between the two is one text message. No tool-call events, because
    no tool ran, and inventing a `delete_tasks` result the model never requested
    would put a fabricated call in the transcript the Run Inspector renders.

    **The `except` is not optional, and its absence was a real defect.** The
    model path reaches AG-UI through `transform_stream`, which converts a raised
    exception into a protocol error event. This path does not: it hands
    already-protocol-level events to `streaming_response`, and the pinned 2.27.0
    `encode_stream` is a bare `async for` that encodes what it is given. An
    exception escaping here therefore aborts the SSE body mid-stream after
    `RUN_STARTED`, leaving the browser with a truncated response and no run
    lifecycle event to react to.

    So the failure is stated in the protocol instead:

    ```text
    success    RUN_STARTED -> TEXT_MESSAGE_* -> RUN_FINISHED
    failure    RUN_STARTED -> RUN_ERROR      -> end of stream
    ```

    `RUN_FINISHED` is never emitted after `RUN_ERROR`. The two are alternative
    terminal events, and emitting both would tell the browser the run finished
    normally after telling it the run failed.

    The message is deliberately generic. The exception may carry a database
    error, and a protocol event is client-facing text; the detail belongs in the
    run's stored `error` and the server log, both of which are actor scoped.
    """
    yield RunStartedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)

    try:
        outcome = await run_in_threadpool(
            _run_control_turn, created, user_message, previous_run_id
        )
    except Exception:
        log.exception(
            "control turn failed", extra={"trellis_run_id": str(created.id)}
        )
        yield RunErrorEvent(
            message=(
                "The action did not finish normally. Current task state may "
                "have changed, so the board will refresh from committed state."
            ),
            code="TRELLIS_CONTROL_FAILURE",
        )
        return

    message_id = str(uuid4())
    yield TextMessageStartEvent(message_id=message_id, role="assistant")
    yield TextMessageContentEvent(message_id=message_id, delta=outcome.text)
    yield TextMessageEndEvent(message_id=message_id)
    yield RunFinishedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)


def _run_control_turn(
    created: runs.CreatedTurn,
    user_message: str,
    previous_run_id: UUID | None,
) -> _ControlOutcome:
    """Perform the undo, persist the turn, and complete the run. In that order.

    The split between the two `try` blocks is the D-76 failure contract, and it
    mirrors `_record_failure` on the model path rather than inventing a second
    story about post-commit failure.

    Before compensation, a failure is an ordinary failed run: nothing committed,
    so nothing has to be explained.

    After compensation, PostgreSQL has already moved and the kernel is not
    reopened to take it back. Undo is all-or-nothing within its own transaction;
    it is not part of a larger distributed transaction with this run's history,
    and pretending otherwise by attempting an automatic redo would be a second
    uncontrolled mutation on an error path. So the committed fact is recorded in
    the run's error, the run fails, and the browser refetches the board. A retry
    of the same command cannot double-apply, because the target now carries a
    compensation wave and `attempt_run_undo` refuses it.
    """
    try:
        outcome = _undo_previous_outcome(previous_run_id)
    except Exception as exc:
        _record_control_failure(created.id, str(exc), exc)
        raise

    try:
        runs.complete_control_turn(
            created.id,
            to_jsonable_python(
                _control_history(created.message_history, user_message, outcome.text)
            ),
        )
    except Exception as exc:
        stored_error = (
            f"mutation_committed={str(outcome.mutation_committed).lower()}; "
            f"response_error={exc}"
        )
        _record_control_failure(created.id, stored_error, exc)
        raise

    return outcome


def _record_control_failure(run_id: UUID, stored_error: str, cause: Exception) -> None:
    """Mark the control run failed, without ever masking why it failed.

    Two rules, and the second one is why this is a function rather than three
    lines at each call site.

    The write is guarded, so cleanup cannot overwrite a run that already reached
    a terminal status. A zero-row result means something else finished this run
    first and its outcome stands.

    If the write itself fails, the caller's original exception is the one that
    propagates. When writes are failing broadly the second exception is a
    symptom and the first is the reason the turn failed, so raising the symptom
    would hide the cause. The secondary error is not discarded though: it is
    logged and attached to the primary exception as a note, so a reader sees
    both and neither is mistaken for the other.

    The run is then left non-terminal. Nothing can write a status while writes
    are failing, so that is recorded rather than repaired. It is a pre-existing
    property of every Trellis run, not something this path introduces, and
    general cleanup of abandoned non-terminal runs is a separate decision.
    """
    try:
        runs.fail_run_if_running(run_id, stored_error)
    except Exception as persistence_error:
        log.exception(
            "could not persist the terminal failure for control run %s", run_id
        )
        cause.add_note(
            f"secondary failure while persisting FAILED state: {persistence_error!r}"
        )


def _undo_previous_outcome(previous_run_id: UUID | None) -> _ControlOutcome:
    """Resolve the target and attempt the undo. The model contributes nothing.

    There is no backward search. If the nominated run cannot be undone, that is
    the answer; this never walks to an older run to find one that can, and it
    never falls back to the continuity run, because both would undo something
    the user did not name.

    `OutOfScopeError` and an absent locator produce the same sentence, so a
    forged id belonging to another actor is indistinguishable from no id at all.
    """
    if previous_run_id is None:
        return _ControlOutcome(text=_NO_TARGET_TEXT)

    try:
        attempt = runs.attempt_run_undo(previous_run_id, settings.actor_id)
    except OutOfScopeError:
        return _ControlOutcome(text=_NO_TARGET_TEXT)

    if attempt.ineligible is not None:
        return _ControlOutcome(text=_UNDO_INELIGIBLE_TEXT[attempt.ineligible])

    result = attempt.result
    if result.refused:
        return _ControlOutcome(text=_UNDO_REFUSED_TEXT[result.reason])

    changes = "change" if result.applied == 1 else "changes"
    return _ControlOutcome(
        text=(
            f"Undone. I reversed {result.applied} task {changes} from the "
            "previous action, restoring each task under its original id."
        ),
        applied=result.applied,
        mutation_committed=result.applied > 0,
    )


def _control_history(inherited: list, user_message: str, response_text: str) -> list:
    """The canonical history of a control turn, built by the application itself.

    No `Agent` ran, so there is no `RunResult` and `_completion_recorder` has
    nothing to record. These two messages are written directly instead, which is
    sound because Pydantic AI's messages are ordinary dataclasses and its
    documented persistence boundary is exactly the `to_jsonable_python` ->
    storage -> `ModelMessagesTypeAdapter` round trip this build already uses.

    What is deliberately absent is as important as what is present:

        no ToolCallPart      no tool was requested
        no ToolReturnPart    no tool executed
        no ThinkingPart      nothing reasoned, and prior reasoning does not
                             belong in a later turn's history in any case
        no model_name        no provider produced this
        no provider_name     the same, said in the other field
        no framework run id  no framework invocation happened

    Every one of those left unset is a claim not made. A synthetic response
    carrying NVIDIA's model name would make the stored transcript assert a
    request that was never sent.
    """
    history = ModelMessagesTypeAdapter.validate_python(inherited)
    history.append(
        ModelRequest(
            parts=[UserPromptPart(content=user_message)],
            metadata={_CONTROL_METADATA_KEY: _CONTROL_UNDO_PREVIOUS},
        )
    )
    history.append(
        ModelResponse(
            parts=[TextPart(content=response_text)],
            metadata={_CONTROL_METADATA_KEY: _CONTROL_UNDO_PREVIOUS},
        )
    )
    return history


def _control_history_predecessor(
    previous_run_id: UUID | None, continuity_run_id: UUID | None
) -> UUID | None:
    """Which run's canonical history the control turn inherits. Not the target.

    Preferring the previous run reads better in the transcript: the control turn
    then directly follows the action it is reversing. But D-67 requires a history
    predecessor to be completed, and that requirement is not negotiable here,
    because the whole reason `previousRunId` exists is that it may name a failed
    or interrupted run.

    So the fallback is by status, not by eligibility. A completed previous run is
    used even when it turns out not to be undoable, because it is still the right
    conversational predecessor and the refusal belongs in that context. A failed
    or interrupted one supplies no history and the continuity run does instead.
    Neither substitution changes the undo target, which was already fixed.

    A previous run this actor does not own contributes nothing here and is not
    disclosed. The undo attempt refuses it separately, with the same sentence a
    missing one gets.

    Both candidates are checked rather than only the first, and the continuity
    one is checked here rather than being handed to `create_control_turn` to
    refuse. That difference is the whole point. On the ordinary model path a
    stale or foreign continuity locator is correctly a refusal, because the turn
    it would seed is the entire request. Here the request is a mutation, and
    D-76 rules that missing conversation history must never remove valid
    mutation authority. So an unusable history predecessor degrades to a root
    control turn instead of refusing an undo the user is entitled to.
    """
    for candidate in (previous_run_id, continuity_run_id):
        if candidate is not None and _is_completed_and_owned(candidate):
            return candidate
    return None


def _is_completed_and_owned(run_id: UUID) -> bool:
    """Whether this run can serve as a D-67 history predecessor.

    Answering it here rather than letting the insert refuse is what lets the
    caller degrade instead of failing. The refusal it would otherwise raise is
    unchanged and still lives in `create_control_turn`.
    """
    try:
        return runs.load(run_id, settings.actor_id).status is RunStatus.COMPLETED
    except OutOfScopeError:
        return False


def _control_run_input(run_id: UUID) -> RunAgentInput:
    """The identifiers the control stream emits, and nothing else.

    `AGUIEventStream` reads `thread_id` and `run_id` off this to stamp
    `RUN_STARTED` and `RUN_FINISHED`, which is the only reason it exists on a
    path with no model. Same split as every other route: `thread_id` is the
    server-issued application run, `run_id` is a fresh transport invocation id
    and is never application authority.

    The message, tool, state, and context fields are empty because nothing reads
    them here. No adapter is built, so there is no payload for a model to see.
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

    if _CONTINUITY_RUN_KEY not in forwarded:
        return None

    value = forwarded[_CONTINUITY_RUN_KEY]

    if not isinstance(value, str):
        raise OutOfScopeError()

    try:
        return UUID(value)
    except ValueError:
        raise OutOfScopeError() from None


def _accepted_previous_run_id(run_input: RunAgentInput) -> UUID | None:
    """Extract D-76's previous-run lookup key, on the same terms as continuity.

    Separate from `_accepted_continuity_run_id` because the two answer different
    questions, and reading one for both is the defect D-76 exists to prevent.
    The refusal shape is identical and deliberately so: absent is None, a
    non-string is out of scope, and an unparseable UUID is out of scope.

    Extracting it here does not admit it anywhere else. `_accepted_run_input`
    still rebuilds `forwarded_props={}`, so neither locator reaches the adapter
    or the model on any path.
    """
    forwarded = run_input.forwarded_props or {}

    if _PREVIOUS_RUN_KEY not in forwarded:
        return None

    value = forwarded[_PREVIOUS_RUN_KEY]

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
            # D-74. The byte ceiling in `limits.py` bounds the request; this
            # bounds the one value out of it that becomes prompt text and
            # `agent_runs.prompt`. Refused rather than truncated: a model
            # acting on half an instruction is worse than a visible refusal.
            if len(text) > limits.BROWSER_USER_MESSAGE_MAX_CHARS:
                raise ValidationFailedError(
                    "AG-UI user message exceeds the accepted size"
                )
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


def _project_prior_turn_history_for_model(
    history: list[ModelMessage],
) -> list[ModelMessage]:
    """Drop inherited reasoning before a new user turn reaches the provider.

    D-81. Nemotron separates two cases, and Trellis was collapsing them. Inside
    one user turn, reasoning followed by a tool call, a tool result, and more
    reasoning is a single trajectory, and the earlier reasoning is context the
    model should still see. Across user turns it is not: NVIDIA's guidance is
    that a new turn should not be handed the previous turn's `reasoning`.

    D-67 makes a successor run inherit its predecessor's canonical history
    wholesale, which is right for the durable record and wrong as model input,
    because it carries obsolete `ThinkingPart`s across exactly that boundary.

    So this projects the model's view and nothing else. The predecessor row is
    untouched, `runs.create_turn` still owns what is stored, and the browser
    still contributes no history. Only the value handed to `run_stream_native`
    on a new ordinary turn differs.

    It is deliberately not registered as an agent-wide history processor. Such a
    processor runs before every model request, including the one after a tool
    result, so it could not tell inherited reasoning from the reasoning this
    turn just produced, and it would strip both. The distinction is the point,
    so the filter is applied once, here, where "a new turn is starting" is known.

    Structural, not textual: parts are matched by type, never by searching for
    thinking markup. Messages are rebuilt rather than mutated, because the
    inherited objects are shared with the durable snapshot.
    """
    projected: list[ModelMessage] = []

    for message in history:
        if not isinstance(message, ModelResponse):
            projected.append(message)
            continue

        parts = [part for part in message.parts if not isinstance(part, ThinkingPart)]
        if len(parts) == len(message.parts):
            projected.append(message)
            continue

        # A response that was only reasoning has nothing left to say to the
        # model. Dropping it is honest; inventing empty text would not be.
        if parts:
            projected.append(replace(message, parts=parts))

    return projected


@contextmanager
def _model_facing_refusals():
    """Keep the refusals a model can act on inside Pydantic AI's tool protocol.

    Pydantic AI recognises exactly two application-level outcomes from a tool
    body. `ModelRetry` returns a retry prompt so the model can correct the call,
    and `ToolFailed` returns a definitively failed result the model can adapt to
    without spending retry budget. Anything else is not a tool outcome at all:
    it leaves the protocol, aborts the run, and takes the response stream with
    it.

    Trellis raised ordinary application exceptions for refusals that are a
    normal part of a model driving a mutating API, so a stale `expected_version`
    ended the turn instead of being corrected. That is what this fixes, and it
    fixes it here rather than in the domain, because this is the one layer that
    knows its caller is a model. `domain` and `tools` keep raising exactly what
    they raised, and every deterministic caller keeps reading that contract.

    Three refusals are translated, and the choice of outcome is the point:

        VERSION_CONFLICT      ModelRetry. The request may still be valid and
                              only the concurrency token is stale, so the next
                              action is known: look the task up again.

        EXTERNAL_DIVERGENCE   ToolFailed. The disagreement is with Linear, not
                              with the arguments, so a corrected retry would
                              earn the identical refusal.

        OUT_OF_SCOPE          ToolFailed. The reference cannot be used, and
                              retrying the same identifier cannot change that.

    Everything else propagates unchanged, deliberately. A blanket
    `except PolicyError` or `except Exception` here would turn bugs, outages,
    and serialization failures into a polite suggestion to try again, hiding the
    failures most worth seeing and inviting a loop on them. A refusal earns a
    translation only when its next action is known.
    """
    try:
        yield
    except AppendNotesLimitError as exc:
        # D-80, and it must be caught before VersionConflictError's sibling
        # handlers for the obvious reason that ordering decides which one wins;
        # it is unrelated to them, but it is a subclass of ValidationFailedError
        # and the narrower type has to be named first.
        #
        # Terminal rather than retryable. The model cannot correct this without
        # editing the user's text: truncating, summarising, or dropping part of
        # it would all "succeed" while storing something the user did not ask
        # for. An oversized fragment is a different case and Pydantic already
        # rejects that against the schema before this code runs.
        raise ToolFailed(
            "The note text cannot be added because the resulting note would "
            "exceed the maximum length. Nothing was changed. Do not shorten, "
            "summarise, or rewrite the user's text to make it fit; tell them the "
            "note is too long and let them decide."
        ) from exc
    except BulkTargetCoverageError as exc:
        # D-80, and likewise before VersionConflictError, which it subclasses.
        #
        # The generic version-conflict advice is wrong here. That advice tells
        # the caller to resolve the task and retry with a fresh current_version,
        # which is exactly right for `update_task`, whose caller supplied one.
        # A bulk call supplies none: the server reads each version after locking
        # the row. Repeating that advice would ask the model to manufacture a
        # value it never sent.
        raise ToolFailed(
            "The bulk operation could not safely cover its whole target set, so "
            "no partial change was committed. Do not invent expected versions "
            "and do not retry blindly. Look up the current tasks again if the "
            "user still wants this change."
        ) from exc
    except VersionConflictError as exc:
        # No new version travels in this message. Handing one back would make an
        # exception payload a second source of task state competing with the
        # resolver, and it would be wrong anyway if the row was deleted rather
        # than merely updated.
        raise ModelRetry(
            "The task changed since this call was prepared, so it was refused "
            "rather than applied on top of another change. Resolve the task "
            "reference again now and retry using the current_version that "
            "lookup returns. Do not reuse a version from earlier in this "
            "conversation, and do not guess the next version."
        ) from exc
    except ExternalDivergenceError as exc:
        raise ToolFailed(
            "This task has diverged from its Linear state. Do not retry this "
            "operation until that divergence has been reconciled."
        ) from exc
    except OutOfScopeError as exc:
        # Deliberately generic. A missing task and another actor's task are the
        # same refusal by design, and saying which one this is would turn the
        # model into an oracle for which task ids exist.
        raise ToolFailed(
            "That task reference is not available for this operation. Do not "
            "retry the same identifier blindly. Resolve the user's task "
            "reference again, or explain that the requested task could not be "
            "used."
        ) from exc


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
        # Fact 3 fixes the serialization: produce the JSON-compatible array and
        # store it in the jsonb column. This replaces rather than appends,
        # because `all_messages()` already contains the history this invocation
        # was given. `to_jsonable_python` reaches that same array directly,
        # where dumping to JSON bytes and parsing them back builds one complete
        # intermediate encoding this path never reads.
        messages = to_jsonable_python(result.all_messages())
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
