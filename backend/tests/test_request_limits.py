"""D-74 deterministic resource bounds: transport admission and typed input size.

Two ceilings that protect different resources and must not be conflated:

    transport bytes   how much of a request body may ever be buffered
    typed size        how much accepted content a validated field may carry

A field limit alone is not a bound. `CreateRunRequest.user_message` capped at
8,000 characters says nothing about how many megabytes FastAPI buffered before
Pydantic saw the field, which is why every body-bearing route is admitted
through one byte ceiling first.

The Linear ceiling is deliberately larger and deliberately separate. Trellis
controls the shape of its own browser requests; it does not control what Linear
assembles into `promptContext`. That asymmetry is named rather than hidden by
raising every route to the larger number.

Nothing here reaches a model, a provider, or the network.
"""

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from ag_ui.core import RunAgentInput, UserMessage
from fastapi.testclient import TestClient
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import agent as agent_module
from app import limits, linear_agent, linear_agent_worker, linear_install, sql
from app import linear_agent_api as provider
from app.db import pool
from app.errors import ValidationFailedError
from app.main import app
from app.models import (
    BulkUpdateTasksArgs,
    CreateRunRequest,
    CreateTaskArgs,
    DeleteTasksArgs,
    ProposePlanArgs,
    UpdateTaskArgs,
)


WEBHOOK_SECRET = "test-webhook-secret"
CLIENT_ID = "oauth-client-1"
ORG_ID = "org-1"
APP_USER_ID = "app-user-1"
ALLOWED_HUMAN = "human-allowed"


@pytest.fixture(autouse=True)
def clean_state():
    with pool.connection() as conn:
        conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
        conn.commit()
    yield
    with pool.connection() as conn:
        conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
        conn.commit()


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    replacement = linear_agent.settings.model_copy(
        update={
            "linear_webhook_secret": WEBHOOK_SECRET,
            "linear_client_id": CLIENT_ID,
            "linear_allowed_user_id": ALLOWED_HUMAN,
            "trellis_public_origin": "https://demo.example",
            "linear_client_secret": "client-secret",
            "linear_webhook_freshness_seconds": 60,
        }
    )
    monkeypatch.setattr(linear_agent, "settings", replacement)
    monkeypatch.setattr(linear_install, "settings", replacement)
    monkeypatch.setattr(provider, "settings", replacement)
    return replacement


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------------- helpers


def _sized_json(target: int, base: dict, key: str) -> bytes:
    """A JSON body of exactly `target` bytes, padded through one string field."""
    padded = dict(base)
    padded[key] = ""
    overhead = len(json.dumps(padded, separators=(",", ":")).encode("utf-8"))
    padded[key] = "x" * (target - overhead)
    body = json.dumps(padded, separators=(",", ":")).encode("utf-8")
    assert len(body) == target, (len(body), target)
    return body


def _error_code(response) -> str:
    return response.json()["error"]["code"]


def _error_message(response) -> str:
    return response.json()["error"]["message"]


def _agui_body(message: str) -> dict:
    return {
        "threadId": "client-thread-that-names-no-run",
        "runId": "client-run-that-names-no-run",
        "state": None,
        "messages": [{"id": "m1", "role": "user", "content": message}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def _signed_webhook(client, body: bytes, *, secret: str = WEBHOOK_SECRET):
    headers = {
        "Linear-Delivery": str(uuid4()),
        "Linear-Signature": hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest(),
    }
    return client.post("/api/linear/webhook", content=body, headers=headers)


def _linear_body(target: int) -> bytes:
    base = {
        "type": linear_agent.TYPE_AGENT_SESSION,
        "action": linear_agent.ACTION_CREATED,
        "organizationId": ORG_ID,
        "oauthClientId": CLIENT_ID,
        "appUserId": APP_USER_ID,
        "createdAt": "2026-08-17T12:00:00.000Z",
        "webhookTimestamp": time.time() * 1000,
        "agentSession": {
            "id": "sess-1",
            "organizationId": ORG_ID,
            "appUserId": APP_USER_ID,
            "creatorId": ALLOWED_HUMAN,
        },
    }
    return _sized_json(target, base, "promptContext")


def _inbox_count() -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM linear_agent_inbox").fetchone()
        conn.commit()
    return row["n"]


def _run_count() -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM agent_runs").fetchone()
        conn.commit()
    return row["n"]


# ------------------------------------------------- transport: default ceiling


def test_body_at_the_default_ceiling_is_admitted(client):
    """Exactly 256 KiB passes transport admission and is judged by the route."""
    body = b"z" * limits.DEFAULT_MAX_BODY_BYTES
    response = client.post("/api/demo/reset", content=body)

    assert response.status_code == 422
    # Admitted by transport, then refused by the route's own bodyless contract.
    assert _error_message(response) == "request body must be empty"


def test_one_byte_past_the_default_ceiling_is_refused(client):
    body = b"z" * (limits.DEFAULT_MAX_BODY_BYTES + 1)
    response = client.post("/api/demo/reset", content=body)

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert _error_message(response) == limits.BODY_TOO_LARGE_MESSAGE


def test_oversized_pydantic_body_keeps_the_closed_error_envelope(client):
    """The envelope must not degrade to FastAPI's own body-parse failure.

    An exception raised while FastAPI is parsing a declared body is converted
    by the framework into `400 {"detail": ...}`, which is outside the closed
    vocabulary section 6 fixes. Admission therefore refuses before parsing
    begins rather than raising into it.
    """
    body = _sized_json(
        limits.DEFAULT_MAX_BODY_BYTES + 1, {"user_message": ""}, "user_message"
    )
    response = client.post(
        "/api/runs", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert _error_message(response) == limits.BODY_TOO_LARGE_MESSAGE
    assert "detail" not in response.json()


def test_the_approval_decision_route_is_admitted_like_every_other(client):
    """The fifth body-bearing route, and the second with a declared body.

    Admission is one app-wide middleware with no per-route registration, so
    this route is covered by construction rather than by anything specific to
    it. That is exactly why it is worth pinning: "covered by construction" is a
    claim about the shape of the code, and a later change that made admission
    route-aware would break it silently everywhere this suite does not look.

    The run id names no run. It does not need to: the refusal happens before
    the route body runs, so ownership is never resolved and no row is read.
    """
    body = _sized_json(
        limits.DEFAULT_MAX_BODY_BYTES + 1, {"decision": "approved"}, "padding"
    )
    response = client.post(
        f"/api/runs/{uuid4()}/approvals/call-that-names-nothing",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert _error_code(response) == "VALIDATION_ERROR"
    assert _error_message(response) == limits.BODY_TOO_LARGE_MESSAGE
    assert "detail" not in response.json()


def test_dishonest_content_length_cannot_buy_an_unbounded_body(client):
    """The streamed byte count is the authority, not the declared header."""
    body = b"z" * (limits.DEFAULT_MAX_BODY_BYTES + 1)
    response = client.post(
        "/api/demo/reset", content=body, headers={"Content-Length": "5"}
    )

    assert response.status_code == 422
    assert _error_message(response) == limits.BODY_TOO_LARGE_MESSAGE


def test_absent_content_length_cannot_buy_an_unbounded_body(client):
    """A chunked body declares no length at all and is still bounded."""

    def chunks():
        for _ in range(9):
            yield b"z" * (limits.DEFAULT_MAX_BODY_BYTES // 8)

    response = client.post("/api/demo/reset", content=chunks())

    assert response.status_code == 422
    assert _error_message(response) == limits.BODY_TOO_LARGE_MESSAGE


def test_ordinary_requests_are_unaffected(client):
    """Admission must be invisible to every request that is not oversized."""
    assert client.get("/api/tasks").status_code == 200
    assert client.post("/api/demo/reset").status_code == 200


# -------------------------------------------------- transport: Linear ceiling


def test_linear_body_at_its_own_ceiling_is_admitted(client, monkeypatch):
    seen: list[bytes] = []
    original = linear_agent.verify_signature

    def recording(raw_body, header):
        seen.append(raw_body)
        return original(raw_body, header)

    monkeypatch.setattr(linear_agent, "verify_signature", recording)

    body = _linear_body(limits.LINEAR_WEBHOOK_MAX_BODY_BYTES)
    response = _signed_webhook(client, body)

    assert response.status_code != 422
    assert seen, "an admitted webhook must reach signature verification"


def test_one_byte_past_the_linear_ceiling_is_refused(client):
    body = _linear_body(limits.LINEAR_WEBHOOK_MAX_BODY_BYTES + 1)
    response = _signed_webhook(client, body)

    assert response.status_code == 422
    assert _error_message(response) == limits.BODY_TOO_LARGE_MESSAGE


def test_the_default_ceiling_does_not_apply_to_the_linear_route(client):
    """A payload larger than 256 KiB is ordinary for a provider-assembled body."""
    body = _linear_body(limits.DEFAULT_MAX_BODY_BYTES + 1)
    response = _signed_webhook(client, body)

    assert response.status_code != 422


def test_admitted_bytes_reach_signature_verification_unchanged(client, monkeypatch):
    """The HMAC covers the received bytes, so replay must not alter one of them."""
    seen: list[bytes] = []
    monkeypatch.setattr(
        linear_agent,
        "verify_signature",
        lambda raw_body, header: seen.append(raw_body),
    )

    body = _linear_body(limits.DEFAULT_MAX_BODY_BYTES + 17)
    _signed_webhook(client, body)

    assert len(seen) == 1
    assert hashlib.sha256(seen[0]).hexdigest() == hashlib.sha256(body).hexdigest()
    assert seen[0] == body


def test_oversized_webhook_produces_no_durable_work(client):
    body = _linear_body(limits.LINEAR_WEBHOOK_MAX_BODY_BYTES + 1)
    response = _signed_webhook(client, body)

    assert response.status_code == 422
    assert _inbox_count() == 0
    assert _run_count() == 0


def test_oversized_agui_request_opens_no_application_run(client):
    body = _sized_json(
        limits.DEFAULT_MAX_BODY_BYTES + 1, _agui_body(""), "extraPadding"
    )
    response = client.post(
        "/api/agui", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert _error_message(response) == limits.BODY_TOO_LARGE_MESSAGE
    assert _run_count() == 0


# ------------------------------------------------------ accepted message size


def test_browser_user_message_at_the_ceiling_is_accepted():
    message = "a" * limits.BROWSER_USER_MESSAGE_MAX_CHARS
    assert CreateRunRequest(user_message=message).user_message == message


def test_browser_user_message_past_the_ceiling_is_refused():
    with pytest.raises(ValidationError):
        CreateRunRequest(user_message="a" * (limits.BROWSER_USER_MESSAGE_MAX_CHARS + 1))


def _run_input(message: str) -> RunAgentInput:
    return RunAgentInput(
        thread_id="t",
        run_id="r",
        state=None,
        messages=[UserMessage(id="m1", role="user", content=message)],
        tools=[],
        context=[],
        forwarded_props={},
    )


def test_agui_newest_message_at_the_ceiling_is_accepted():
    message = "a" * limits.BROWSER_USER_MESSAGE_MAX_CHARS
    assert agent_module._accepted_user_message(_run_input(message)) == message


def test_agui_newest_message_past_the_ceiling_is_refused():
    oversized = "a" * (limits.BROWSER_USER_MESSAGE_MAX_CHARS + 1)
    with pytest.raises(ValidationFailedError):
        agent_module._accepted_user_message(_run_input(oversized))


def _prompted_payload(body: str) -> dict:
    return {
        "action": linear_agent.ACTION_PROMPTED,
        "agentActivity": {"content": {"type": "prompt", "body": body}},
    }


def _created_payload(context: str) -> dict:
    return {"action": linear_agent.ACTION_CREATED, "promptContext": context}


def test_linear_prompted_message_at_the_ceiling_is_accepted():
    body = "a" * limits.LINEAR_PROMPTED_MESSAGE_MAX_CHARS
    assert linear_agent_worker.extract_prompt(_prompted_payload(body)) == body


def test_linear_prompted_message_past_the_ceiling_is_refused():
    body = "a" * (limits.LINEAR_PROMPTED_MESSAGE_MAX_CHARS + 1)
    with pytest.raises(linear_agent_worker.PromptTooLarge):
        linear_agent_worker.extract_prompt(_prompted_payload(body))


def test_linear_created_context_at_the_ceiling_is_accepted():
    """A provider-assembled context is judged against its own larger ceiling."""
    context = "a" * limits.LINEAR_CREATED_CONTEXT_MAX_CHARS
    assert linear_agent_worker.extract_prompt(_created_payload(context)) == context


def test_linear_created_context_past_the_ceiling_is_refused():
    context = "a" * (limits.LINEAR_CREATED_CONTEXT_MAX_CHARS + 1)
    with pytest.raises(linear_agent_worker.PromptTooLarge):
        linear_agent_worker.extract_prompt(_created_payload(context))


def test_the_prompted_ceiling_is_not_applied_to_created_context():
    """The two actions carry materially different inputs. See D-74."""
    context = "a" * (limits.LINEAR_PROMPTED_MESSAGE_MAX_CHARS + 1)
    assert linear_agent_worker.extract_prompt(_created_payload(context)) == context


# ------------------------------------------------------- typed tool arguments


def test_notes_at_the_ceiling_are_accepted():
    notes = "n" * limits.TASK_NOTES_MAX_CHARS
    assert CreateTaskArgs(title="t", notes=notes).notes == notes
    assert (
        UpdateTaskArgs(task_id=uuid4(), expected_version=1, notes=notes).notes
        == notes
    )


def test_notes_past_the_ceiling_are_refused():
    notes = "n" * (limits.TASK_NOTES_MAX_CHARS + 1)
    with pytest.raises(ValidationError):
        CreateTaskArgs(title="t", notes=notes)
    with pytest.raises(ValidationError):
        UpdateTaskArgs(task_id=uuid4(), expected_version=1, notes=notes)


def test_bulk_update_target_count_is_bounded():
    ids = [uuid4() for _ in range(limits.BULK_TASK_IDS_MAX)]
    assert len(BulkUpdateTasksArgs(task_ids=ids, notes="x").task_ids) == len(ids)
    with pytest.raises(ValidationError):
        BulkUpdateTasksArgs(task_ids=ids + [uuid4()], notes="x")


def test_delete_target_count_is_bounded():
    ids = [uuid4() for _ in range(limits.DELETE_TASK_IDS_MAX)]
    assert len(DeleteTasksArgs(task_ids=ids).task_ids) == len(ids)
    with pytest.raises(ValidationError):
        DeleteTasksArgs(task_ids=ids + [uuid4()])


def test_plan_summary_is_bounded():
    summary = "s" * limits.PLAN_SUMMARY_MAX_CHARS
    assert ProposePlanArgs(summary=summary, steps=["a"]).summary == summary
    with pytest.raises(ValidationError):
        ProposePlanArgs(summary="s" * (limits.PLAN_SUMMARY_MAX_CHARS + 1), steps=["a"])


def test_plan_step_count_is_bounded():
    steps = ["step"] * limits.PLAN_STEPS_MAX
    assert len(ProposePlanArgs(summary="s", steps=steps).steps) == len(steps)
    with pytest.raises(ValidationError):
        ProposePlanArgs(summary="s", steps=steps + ["one too many"])


def test_each_plan_step_is_bounded():
    step = "s" * limits.PLAN_STEP_MAX_CHARS
    assert ProposePlanArgs(summary="s", steps=[step]).steps == [step]
    with pytest.raises(ValidationError):
        ProposePlanArgs(summary="s", steps=["s" * (limits.PLAN_STEP_MAX_CHARS + 1)])
