"""The real Pydantic AI agent and the production AG-UI transport, from T12A.

Gate A already proved the transport and interrupt shapes on Day 1. This module
does not re-answer that question. It wires the proven shape to the real agent,
the real six tools, the real prompt boundary, the real `agent_runs` record, and
the real server-owned history, without letting the browser become an authority
for any of them.

**The whole trust boundary is one function, `_accepted_run_input`.** An AG-UI
client sends its entire transcript on every request, and `UIAdapter` appends
whatever `self.messages` yields to the `message_history` a caller supplies. So
filtering after the adapter is built would be filtering downstream of the thing
that reads the payload. Instead the incoming `RunAgentInput` is discarded and a
new one is constructed from scratch, carrying exactly one field taken from the
client: the newest user message. Every other field on the rebuilt input is
server-chosen or empty.

That is a property a reader can check by grep rather than by reasoning about a
resolver's branches, which matters because reintroducing client-owned history is
the single most likely way a later change quietly breaks this build:

- `messages`         one accepted user message, nothing else
- `tools`            empty, so no client-declared frontend toolset is registered
- `state`            null, so no client state reaches `deps`
- `context`          empty
- `forwarded_props`  empty
- `resume`           absent, so `AGUIAdapter.deferred_tool_results` is None and
                     a client-asserted approval cannot continue a deferred call.
                     The approval bridge in BUILD_SPEC section 10 requires the
                     server to construct the resume result from its own stored
                     decision, and T12B owns that.
- `thread_id`        the server-issued `agent_runs.id`
- `run_id`           a fresh framework invocation id

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
from dataclasses import dataclass
from functools import cache
from uuid import UUID, uuid4

from ag_ui.core import RunAgentInput, UserMessage
from fastapi import Request
from fastapi.responses import Response
from pydantic_ai import Agent, DeferredToolRequests, RunContext
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models import Model
from pydantic_ai.ui.ag_ui import AGUIAdapter
from starlette.concurrency import run_in_threadpool

from . import domain, prompts, runs, tools
from .config import settings
from .errors import ValidationFailedError
from .models import (
    BulkUpdateTasksArgs,
    CreateTaskArgs,
    DeleteTasksArgs,
    ListTasksArgs,
    ProposePlanArgs,
    RunStatus,
    UpdateTaskArgs,
)


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


def build_agent(model: Model | str | None = None) -> Agent[TrellisDeps]:
    """Construct the agent from the configured runtime model.

    BUILD_SPEC section 1 fixes the construction: `Agent(settings.model_id)`.
    Pydantic AI resolves the provider from the `MODEL_ID` prefix and reads the
    matching credential from the environment, so there is no provider branch
    here, no provider router, no hardcoded model name, and no second selector.

    The `model` parameter is an injection point, not a selector. `MODEL_ID`
    remains the only configured runtime model, and the default path is exactly
    the line section 1 prints. The parameter exists so the deterministic gate can
    drive this identical toolset and prompt against a `FunctionModel`, proving
    the transport and the trust boundary without a provider call. BUILD_SPEC
    keeps model behaviour in the eval suite, which is where a provider belongs.
    """
    agent = Agent(
        settings.model_id if model is None else model,
        deps_type=TrellisDeps,
        instructions=prompts.SYSTEM_PROMPT,
        # `DeferredToolRequests` is what lets an approval-required call surface
        # as an AG-UI interrupt rather than raise. It is transport shape, proven
        # at Gate A. It is not the approval bridge: T12A writes no approval row
        # and honours no client decision. See the limitations in
        # IMPLEMENTATION_NOTES.md.
        output_type=[str, DeferredToolRequests],
    )

    # The six tools from BUILD_SPEC section 10. Each wrapper does exactly one
    # thing: translate `RunContext` into `ToolContext` and call the T10 body
    # unchanged. No policy, idempotency, or domain decision is made here. The
    # model chooses which typed tool to request; deterministic code still decides
    # what is allowed and what commits.
    #
    # A single Pydantic model parameter is flattened by Pydantic AI into the
    # tool's JSON schema, so the model sees section 10's explicit fields and
    # enums rather than a nested wrapper object.

    @agent.tool
    def list_tasks(
        ctx: RunContext[TrellisDeps], arguments: ListTasksArgs
    ) -> list[domain.TaskSnapshot]:
        """Read the user's tasks with typed status, date, priority, and limit filters."""
        return tools.list_tasks(_tool_context(ctx), arguments)

    @agent.tool
    def create_task(
        ctx: RunContext[TrellisDeps], arguments: CreateTaskArgs
    ) -> list[domain.TaskSnapshot]:
        """Create one task with typed title, notes, due date, priority, and dependency fields."""
        return tools.create_task(_tool_context(ctx), arguments)

    @agent.tool
    def update_task(
        ctx: RunContext[TrellisDeps], arguments: UpdateTaskArgs
    ) -> list[domain.TaskSnapshot]:
        """Update one task using its identifier and expected version."""
        return tools.update_task(_tool_context(ctx), arguments)

    @agent.tool
    def bulk_update_tasks(
        ctx: RunContext[TrellisDeps], arguments: BulkUpdateTasksArgs
    ) -> list[domain.TaskSnapshot]:
        """Apply the same typed changes to a list of task identifiers."""
        return tools.bulk_update_tasks(_tool_context(ctx), arguments)

    # The one declarative gate. API fact 2 established `requires_approval=True`,
    # and fact 4 established that it accepts only a boolean, which is why
    # `bulk_update_tasks` raises from inside its own body instead. The framework
    # defers this call before the body runs at all, so `delete_tasks` never
    # reaches its own D-12 step 0 on this route.
    @agent.tool(requires_approval=True)
    def delete_tasks(
        ctx: RunContext[TrellisDeps], arguments: DeleteTasksArgs
    ) -> list[domain.TaskSnapshot]:
        """Delete a list of tasks through the required approval path."""
        return tools.delete_tasks(_tool_context(ctx), arguments)

    @agent.tool
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
    a module from requiring a live service. `Agent(settings.model_id)` raises
    when `MODEL_ID` is unset, so constructing it at import would make importing
    `app.main` require a configured runtime model, and every deterministic test
    imports `app.main`.

    Callers reach this through the module attribute rather than a `from` import,
    so a test can substitute a deterministic model without a dependency-injection
    seam in the route.
    """
    return build_agent()


async def handle_agui_request(request: Request) -> Response:
    """`POST /api/agui`. The production AG-UI transport.

    The order below is the contract, and each step is doing one job:

    1. Parse the AG-UI payload into `RunAgentInput`.
    2. Take the newest user message. This is the only value accepted from the
       client anywhere on this route.
    3. Open a server-owned application run from that exact value.
    4. Rebuild the run input from scratch so the adapter can read nothing else.
    5. Load canonical history through the single function section 9 names.
    6. Stream, recording failure, completion, history, and usage server-side.
    """
    run_input = AGUIAdapter.build_run_input(await request.body())
    user_message = _accepted_user_message(run_input)

    # Step 3. `runs.create` is the existing run-creation primitive that
    # `POST /api/runs` already uses, so there is one INSERT and one place that
    # issues an application run id. The AG-UI path does not require the REST
    # endpoint as a preflight, and does not change its contract.
    run = await run_in_threadpool(
        runs.create, settings.actor_id, user_message, settings.model_id
    )

    adapter: AGUIAdapter[TrellisDeps, str | DeferredToolRequests] = AGUIAdapter(
        get_agent(),
        _accepted_run_input(run.id, user_message),
        accept=request.headers.get("accept"),
    )

    # Step 5. Empty for a new run, by construction, and read from the database
    # anyway. The read is not ceremony: it is the same call a T12B continuation
    # makes against a run whose history is not empty, and asserting that it
    # returns canonical state rather than the submitted transcript is exactly
    # what `test_agui_forged_history_ignored` proves.
    history = await run_in_threadpool(runs.load_history, run.id, settings.actor_id)
    message_history = ModelMessagesTypeAdapter.validate_json(json.dumps(history))

    native = adapter.run_stream_native(
        message_history=message_history,
        deps=TrellisDeps(actor_id=settings.actor_id, run_id=run.id),
    )
    events = adapter.transform_stream(
        _record_failure(native, run.id),
        on_complete=_completion_recorder(run.id),
    )
    return adapter.streaming_response(events)


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


async def _record_failure(stream, run_id: UUID):
    """Mark the run failed before the adapter converts the error into an event.

    `UIEventStream.transform_stream` catches exceptions and emits `RUN_ERROR`, so
    an exception raised inside the agent is not visible to a caller wrapping the
    protocol stream. Wrapping the native stream instead puts this ahead of that
    handler. The exception is re-raised, so the client still receives the
    `RUN_ERROR` event it would have received anyway.
    """
    try:
        async for event in stream:
            yield event
    except Exception as exc:
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
        # The run is not finished and must not be marked completed. Moving it to
        # `awaiting_approval` and writing the pending `approvals` row is the
        # approval bridge, which T12B owns; D-50 records that nothing in the
        # application does either today. The run therefore stays `running` until
        # T12B, which is a stated T12A limitation rather than an oversight.
        if isinstance(result.output, DeferredToolRequests):
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
