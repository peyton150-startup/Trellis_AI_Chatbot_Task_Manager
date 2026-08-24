"""D-81. Turn-bounded Nemotron reasoning, and provider limits Trellis owns.

Two things were left to the provider that should not have been.

The first is a boundary. Nemotron separates multi-step from multi-turn: inside
one user turn, reasoning followed by a tool call and its result is a single
trajectory and the earlier reasoning is useful context, while a new user turn
should not be handed the previous turn's reasoning at all. D-67 makes a
successor run inherit its predecessor's canonical history wholesale, which is
right for the durable record and wrong as model input, because it carries
obsolete `ThinkingPart`s across exactly that boundary.

The fix is one projection at one seam. The ordinary new-turn path strips
inherited reasoning before the provider sees it; the approval-continuation path
does not, because a continuation is still the same user turn. The predecessor
row is untouched either way. A blanket agent-wide history processor was
rejected: it runs before every model request, including the one after a tool
result, so it could not tell inherited reasoning from the reasoning this turn
just produced.

The second is a set of limits. Trellis sent only a timeout, leaving NVIDIA's
`reasoning_budget` of 16384, `max_tokens` of 16384, and `temperature` of 1 in
charge. Reasoning stays enabled and is now bounded at 6000 per model request,
with enough total allowance left that a fifty-id tool call still fits.

These tests are ordered as the change reads: what the projection does, where it
is and is not applied, and what the provider actually receives on the wire.
"""

import sys
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai import models
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent as agent_module
from app.agent import _project_prior_turn_history_for_model as project
from app.config import (
    MODEL_OUTPUT_HEADROOM_MIN,
    MODEL_REASONING_BUDGET_CEILING,
    Settings,
    settings,
)

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OLD = "OLD_REASONING"
CURRENT = "CURRENT_REASONING"


# ------------------------------------------------------- the projection


def _prior_turn_history():
    """One completed turn: a question, reasoning, a tool round trip, an answer."""
    return [
        ModelRequest(parts=[UserPromptPart("what are my tasks")]),
        ModelResponse(
            parts=[
                ThinkingPart(OLD),
                ToolCallPart("list_tasks", {"limit": 5}, tool_call_id="call-1"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="list_tasks", content=[], tool_call_id="call-1"
                )
            ]
        ),
        ModelResponse(parts=[TextPart("You have no tasks.")]),
    ]


def test_prior_turn_reasoning_is_removed():
    projected = project(_prior_turn_history())
    parts = [part for message in projected for part in message.parts]
    assert not any(isinstance(part, ThinkingPart) for part in parts)


def test_everything_that_is_not_reasoning_survives():
    """The projection is a filter, not a summariser.

    Dropping a tool call or its return would break the pairing the provider
    relies on, and dropping visible text would lose the conversation.
    """
    projected = project(_prior_turn_history())
    kinds = [type(part).__name__ for message in projected for part in message.parts]

    assert kinds.count("UserPromptPart") == 1
    assert kinds.count("ToolCallPart") == 1
    assert kinds.count("ToolReturnPart") == 1
    assert kinds.count("TextPart") == 1


def test_tool_calls_keep_their_returns_paired():
    projected = project(_prior_turn_history())
    calls = {
        part.tool_call_id
        for message in projected
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }
    returns = {
        part.tool_call_id
        for message in projected
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    assert calls == returns, "a filtered history must not orphan a tool call"


def test_a_response_that_was_only_reasoning_is_dropped_not_emptied():
    """Nothing is invented to fill the hole.

    A placeholder TextPart would put words in the model's mouth that it never
    said, and an empty response says nothing at all.
    """
    history = [
        ModelRequest(parts=[UserPromptPart("hello")]),
        ModelResponse(parts=[ThinkingPart(OLD)]),
        ModelResponse(parts=[TextPart("hi")]),
    ]
    projected = project(history)

    assert len(projected) == 2
    assert all(message.parts for message in projected)
    assert not any(
        isinstance(part, ThinkingPart)
        for message in projected
        for part in message.parts
    )


def test_the_inherited_history_is_not_mutated():
    """The objects come from the durable snapshot and are shared with it.

    Editing `parts` in place would rewrite what the predecessor run recorded,
    which is a different thing entirely from changing what the model is shown.
    """
    history = _prior_turn_history()
    before = [[type(part).__name__ for part in m.parts] for m in history]

    project(history)

    after = [[type(part).__name__ for part in m.parts] for m in history]
    assert after == before
    assert any(isinstance(p, ThinkingPart) for p in history[1].parts)


def test_a_history_with_no_reasoning_is_returned_unchanged():
    history = [
        ModelRequest(parts=[UserPromptPart("hello")]),
        ModelResponse(parts=[TextPart("hi")]),
    ]
    projected = project(history)
    assert projected == history


def test_the_filter_is_structural_rather_than_textual():
    """Reasoning is identified by type, never by hunting for markup.

    Text that merely mentions thinking tags is ordinary content and must
    survive, or a user quoting a prompt could lose their own message.
    """
    history = [
        ModelRequest(parts=[UserPromptPart("what does <think> mean?")]),
        ModelResponse(parts=[TextPart("It marks a reasoning block, like <think>.")]),
    ]
    projected = project(history)

    text = " ".join(
        str(part.content)
        for message in projected
        for part in message.parts
        if isinstance(part, (TextPart, UserPromptPart))
    )
    assert "<think>" in text


# ------------------------------------------------ where it is applied


def test_the_new_turn_path_projects_and_the_continuation_path_does_not():
    """The distinction is the whole decision, so it is read off the source.

    A continuation is still the same user turn: the reasoning that chose the
    approval-required action is context for interpreting its result. Applying
    the projection there would throw that away at exactly the wrong moment.
    """
    import inspect

    new_turn = inspect.getsource(agent_module.handle_agui_request)
    continuation = inspect.getsource(agent_module._handle_continuation)

    assert "_project_prior_turn_history_for_model" in new_turn
    assert "_project_prior_turn_history_for_model" not in continuation


def test_no_agent_wide_history_processor_was_registered():
    """A blanket processor cannot tell inherited reasoning from current reasoning.

    It runs before every model request, including the one after a tool result,
    so it would strip the trajectory's own thinking along with the inherited
    kind. That is the overcorrection this decision exists to avoid.
    """
    import inspect

    source = inspect.getsource(agent_module.build_agent)
    assert "history_processors" not in source


def test_the_projection_runs_after_the_server_owned_history_is_validated():
    """Order matters: the browser still contributes nothing.

    The projection edits what the provider is shown. It must not become a place
    where a submitted transcript could enter, so it sits after the server-owned
    snapshot has been validated.
    """
    import inspect

    source = inspect.getsource(agent_module.handle_agui_request)
    validated = source.index("ModelMessagesTypeAdapter.validate_python")
    projected = source.index("_project_prior_turn_history_for_model")
    assert validated < projected


# ------------------------------------------------------ the wire request


def _capture_request(model_settings, message_history=None, tools=None):
    """Serialize one real request through the pinned client and return its body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        captured["handled_in_process"] = True
        return httpx.Response(
            200,
            json={
                "id": "probe",
                "object": "chat.completion",
                "created": 0,
                "model": "nvidia/probe",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    client = AsyncOpenAI(
        base_url="https://integrate.api.nvidia.com/v1",
        api_key="probe-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    # The suite-wide guard exists to stop a test reaching a real provider. This
    # transport cannot: every request is answered in process by `handler`, and
    # the key is a placeholder. Lifting it here buys the one thing a mock model
    # cannot give, which is the body the real client actually serializes.
    # The production profile, not an equivalent one built here. Mutation testing
    # found the difference: with a copy, removing a field from `_nvidia_profile`
    # left every wire test green, because they were proving what a profile
    # configured this way does rather than what Trellis is configured to do.
    model = OpenAIChatModel(
        "nvidia/probe",
        provider=OpenAIProvider(openai_client=client),
        profile=agent_module._nvidia_profile(),
    )
    agent = Agent(model, model_settings=model_settings)

    original = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = True
    try:
        agent.run_sync("hello", message_history=message_history)
    finally:
        models.ALLOW_MODEL_REQUESTS = original

    assert captured["handled_in_process"], "the probe escaped its mock transport"
    return captured["body"]


def test_the_outgoing_request_carries_the_limits_trellis_owns():
    """Configuration objects are not evidence; the serialized body is.

    Every one of these was a provider default before D-81.
    """
    body = _capture_request(agent_module._model_settings())

    assert body["temperature"] == 0.0
    assert body["max_tokens"] == settings.model_max_tokens == 12288
    assert body["reasoning_budget"] == settings.model_reasoning_budget == 6000
    assert body["chat_template_kwargs"] == {"enable_thinking": True}


def test_the_token_cap_is_sent_under_the_name_nvidia_reads():
    """The failure this prevents is silent.

    Pydantic AI serializes `max_tokens` as `max_completion_tokens` unless the
    profile says the model does not support it, and NVIDIA's hosted Chat
    Completions documents `max_tokens`. Sent under the wrong name the cap is
    ignored, which is indistinguishable from never setting one.
    """
    body = _capture_request(agent_module._model_settings())

    assert "max_tokens" in body
    assert "max_completion_tokens" not in body


def test_top_p_is_left_alone():
    """One sampling knob, deliberately. NVIDIA advises against tuning both."""
    body = _capture_request(agent_module._model_settings())
    assert "top_p" not in body


def test_thinking_is_capped_rather_than_switched_off():
    """The goal is a bounded reasoning step, not a model that stops reasoning."""
    body = _capture_request(agent_module._model_settings())
    assert body["chat_template_kwargs"]["enable_thinking"] is True
    assert body["reasoning_budget"] > 0


def test_prior_turn_reasoning_never_reaches_the_provider():
    """The projection proven where it matters, at the serialization boundary.

    Asserting the helper returned no `ThinkingPart` would not establish this:
    what the provider receives is the only thing NVIDIA reads.
    """
    projected = project(_prior_turn_history())
    body = _capture_request(agent_module._model_settings(), message_history=projected)

    rendered = str(body["messages"])
    assert OLD not in rendered

    assert "what are my tasks" in rendered
    assert "list_tasks" in rendered
    assert "You have no tasks." in rendered


def test_unprojected_history_would_have_sent_the_old_reasoning():
    """The control for the test above.

    Without it, a projection that silently did nothing would look identical to
    one that worked, because both would produce a body with no reasoning if the
    client dropped it for some other reason.
    """
    body = _capture_request(
        agent_module._model_settings(), message_history=_prior_turn_history()
    )
    assert OLD in str(body["messages"]), (
        "the client does drop reasoning on its own, so the projection test above "
        "would prove nothing"
    )


def test_same_turn_reasoning_is_still_sent_back_after_a_tool_result():
    """The overcorrection guard, at the same boundary.

    Inside one trajectory the model's own reasoning is context for reading the
    tool result it asked for. A global `openai_chat_send_back_thinking_parts`
    of False, or a blanket history processor, would remove this and the tests
    above would still pass.
    """
    trajectory = [
        ModelRequest(parts=[UserPromptPart("what are my tasks")]),
        ModelResponse(
            parts=[
                ThinkingPart(CURRENT),
                ToolCallPart("list_tasks", {"limit": 5}, tool_call_id="call-9"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="list_tasks", content=[], tool_call_id="call-9"
                )
            ]
        ),
    ]
    body = _capture_request(agent_module._model_settings(), message_history=trajectory)

    rendered = str(body["messages"])
    assert CURRENT in rendered, "same-turn reasoning was stripped"
    assert "list_tasks" in rendered


def test_the_production_profile_names_the_two_fields_and_no_more():
    """Read off the profile object itself, so a copy cannot stand in for it.

    Turning replay off globally would satisfy the turn-boundary requirement and
    quietly break the multi-step case NVIDIA trains separately, so its absence
    from the profile is a decision worth asserting rather than a coincidence.
    """
    profile = agent_module._nvidia_profile()

    # In pinned 2.27.0 this is a mapping of exactly the fields that were set,
    # which makes "and no more" directly assertable.
    assert profile == {
        "openai_chat_thinking_field": "reasoning",
        "openai_chat_supports_max_completion_tokens": False,
    }

    # Absence is the point for this one: leaving it unset keeps the "auto"
    # default, which is what preserves same-turn replay.
    assert "openai_chat_send_back_thinking_parts" not in profile


def test_the_runtime_model_uses_that_profile():
    """The production constructor and the tested profile are the same object.

    Without this, `_nvidia_profile` could be correct and unused.
    """
    import inspect

    source = inspect.getsource(agent_module._runtime_model)
    assert "_nvidia_profile()" in source


# ------------------------------------------------------- configuration


def test_the_reasoning_ceiling_is_an_application_limit():
    assert MODEL_REASONING_BUDGET_CEILING == 6000
    assert settings.model_reasoning_budget == 6000


@pytest.mark.parametrize("value", [6001, 16384, 0, -1])
def test_a_budget_outside_the_ceiling_is_refused(value):
    """Refused rather than clamped.

    Accepting 8000 and sending 6000 would make the configuration and the wire
    disagree, which nobody notices until they are reading a latency graph that
    makes no sense.
    """
    base = settings.model_dump()
    with pytest.raises(ValidationError):
        Settings(**{**base, "model_reasoning_budget": value})


@pytest.mark.parametrize("value", [1, 2000, 4000, 6000])
def test_a_budget_below_the_ceiling_is_allowed(value):
    """Lower budgets stay configurable so the cap can be measured downwards."""
    base = settings.model_dump()
    Settings(**{**base, "model_reasoning_budget": value})


def test_reasoning_cannot_be_configured_to_consume_the_whole_allowance():
    """The `finish_reason="length"` failure, refused at startup.

    Reasoning and output share one generation budget, so a configuration with no
    headroom produces a model that thought successfully and then had no room to
    answer or call anything.
    """
    base = settings.model_dump()
    with pytest.raises(ValidationError):
        Settings(**{**base, "model_max_tokens": base["model_reasoning_budget"]})

    with pytest.raises(ValidationError):
        Settings(
            **{
                **base,
                "model_max_tokens": base["model_reasoning_budget"]
                + MODEL_OUTPUT_HEADROOM_MIN
                - 1,
            }
        )


def test_the_headroom_fits_the_largest_tool_call_this_system_can_emit():
    """Fifty task ids is the biggest legitimate payload, so it has to fit.

    The floor is asserted as a value, not only as a comparison against today's
    settings. Shrinking the constant while the current numbers still happen to
    satisfy it would otherwise pass, and the constant is the thing that protects
    a future configuration change.
    """
    assert MODEL_OUTPUT_HEADROOM_MIN >= 4096

    headroom = settings.model_max_tokens - settings.model_reasoning_budget
    assert headroom >= MODEL_OUTPUT_HEADROOM_MIN
    assert headroom >= 4096


# ----------------------------------------------------- the tool surface


def test_d81_does_not_change_the_model_capability_surface():
    """A settings change must not quietly alter what the model can do."""
    from pydantic_ai.models.test import TestModel

    from app.models import ToolName

    test_model = TestModel(call_tools=[])
    built = agent_module.build_agent(test_model)
    built.run_sync(
        "hello",
        deps=agent_module.TrellisDeps(actor_id=ACTOR_ID, run_id=uuid4()),
    )
    definitions = {
        tool.name for tool in test_model.last_model_request_parameters.function_tools
    }
    assert definitions == {name.value for name in ToolName}
    assert len(agent_module.ALL_TOOLS) == 8
    assert len(agent_module.LINEAR_TOOLS) == 6


def test_the_routing_kernel_names_the_next_action_not_the_whole_plan():
    """The prompt change is one kernel near the top, not a rewrite.

    The instruction the model needs first is which authoritative action to take
    now, because planning later arguments before the lookup that supplies them
    is what produces guessed ids and stale versions.
    """
    from app import prompts

    text = prompts.SYSTEM_PROMPT.lower()
    assert "next authoritative action" in text
    assert "reason only far enough" in text
    assert "never guess" in text

    # The routes are the kernel's content, not decoration. Losing the bulk
    # append route sends the model back to one update_task per task, which is
    # the failure D-80 exists to remove.
    assert "one bulk_update_tasks call carrying only append_notes" in text
    assert "-> one bulk_update_tasks call" in text
    assert "-> resolve_task_reference first" in text
    assert "-> list_tasks" in text
    # The kernel belongs near the front, before the detailed rules.
    assert text.index("next authoritative action") < len(text) // 3
