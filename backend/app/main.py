"""FastAPI application and the KERNEL wire contract from BUILD_SPEC section 9.

The four rules in section 9's OPUS ONLY block are enforced here, and this is
what each one costs in code:

1. Every request body parses into the exact declared Pydantic model. Every
   request model inherits `TrellisModel`, which sets
   `model_config = ConfigDict(extra="forbid")`, so an undeclared key is a
   parse failure rather than a field the handler has to remember to ignore.

2. An extra key returns 422 and is never merged. Rejection happens during
   parsing, before any handler body runs, so there is no code path on which a
   smuggled key reaches a handler at all. The handler for
   `RequestValidationError` below turns that into the section 6 error envelope
   with code VALIDATION_ERROR, matching the 422 that section 6 assigns.
   D-48 adds the one bodyless POST present at T09: zero bytes continue and any
   bytes raise the same error before reset opens a database transaction.

3. The server never reads message history, tool calls, approvals, or run state
   from a request body. `CreateRunRequest` declares one field, `user_message`.
   No request model in `models.py` carries history, a tool call, an approval, or
   a run status, so there is nothing to read even if a handler tried.

4. A client-supplied run id is a lookup key, not a grant. Every route that takes
   one passes it to `runs.load`, which resolves it against `agent_runs` and
   raises `OutOfScopeError` unless the row exists and belongs to `actor_id`.
   Missing and not-yours are the same rejection, so the API cannot be used to
   discover which run ids exist.

Section 9's table lists seven endpoints. Five are here. The rest belong to the
tasks that own their behavior, under D-44: the approvals decision to T12B with
the approval bridge, and undo to T18. `/api/runs/{id}/resume` is not here and is
not coming, because D-36 credited resume and orphan sweep as removed in full and
spent the 0.25d; section 9's table is corrected to match.

`/api/agui` arrives at T12A and is the one route whose body is not a declared
Pydantic model, because its shape is the AG-UI `RunAgentInput` the protocol
fixes. Rule 1 is met differently there and no less strictly: rather than
declaring which keys are forbidden, the transport constructs the payload the
agent sees from scratch and copies exactly one value across, the newest user
message. Rule 4 does not apply to it at all, because it accepts no run
identifier to resolve. It creates the application run instead. See `agent.py`,
whose module docstring is the argument for why that is the stronger property.

Actor identity is `settings.actor_id`. This build has one actor by design, and
authentication is out of scope for the demo, but every authorization decision
still flows through the actor rather than around it, so introducing real
identity later is a change of where `actor_id` comes from and nothing else.
"""

import json
from datetime import datetime, timezone
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app import agent, domain, linear_agent, linear_install, runs, seed
from app.config import settings
from app.db import pool
from app.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    PolicyError,
    RunStateInvalidError,
    ValidationFailedError,
)
from app.models import (
    ApprovalDecisionRequest,
    ApprovalState,
    CreateRunRequest,
    ListTasksArgs,
    RunCreatedResponse,
    RunDetail,
    RunStatus,
    TasksResponse,
)


app = FastAPI(title="Trellis", version="0.1.0")


async def _require_no_body(request: Request) -> None:
    """D-48: bodyless means zero bytes, and rejection precedes mutation."""
    if await request.body():
        raise ValidationFailedError("request body must be empty")


def _envelope(code: str, message: str) -> dict:
    """The single response shape for every rejection.

    Section 6 fixes a code and an HTTP status per error class. Keeping one
    envelope means a client distinguishes failures by `code` rather than by
    parsing prose, and that the twelve codes are the whole vocabulary.
    """
    return {"error": {"code": code, "message": message}}


@app.exception_handler(PolicyError)
async def handle_policy_error(request: Request, exc: PolicyError) -> JSONResponse:
    """Every rejection the policy layer, lease, or undo raises, mapped by class.

    The status comes off the exception rather than the raise site, so a route
    cannot accidentally return 403 for a conflict. `errors.py` is the only place
    a code and status are paired.
    """
    return JSONResponse(
        status_code=exc.http_status,
        content=_envelope(exc.code, exc.message),
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Rule 2. An undeclared key is a 422 and the request stops here.

    Only the field locations are reported, never the submitted values. The
    rejected body is client-supplied and may be hostile, and echoing it back
    would put untrusted content into a response for no diagnostic gain: the
    client already knows what it sent.
    """
    locations = sorted(
        {
            ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            for error in exc.errors()
        }
    )
    detail = ", ".join(location for location in locations if location)
    message = (
        f"request body does not match its model: {detail}"
        if detail
        else "request body does not match its model"
    )
    return JSONResponse(
        status_code=ValidationFailedError.http_status,
        content=_envelope(ValidationFailedError.code, message),
    )


@app.get("/api/tasks", response_model=TasksResponse)
def get_tasks() -> TasksResponse:
    """The board. Scoped to the actor by the SQL, not by a filter in Python."""
    with pool.connection() as conn:
        tasks = domain.list_tasks(settings.actor_id, ListTasksArgs(), conn=conn)
        conn.commit()
    return TasksResponse(tasks=tasks)


@app.post("/api/runs", response_model=RunCreatedResponse, status_code=201)
def post_run(body: CreateRunRequest) -> RunCreatedResponse:
    """Open an application run and return the id the server issued.

    The run record is created here and the agent is not invoked, because
    `agent.py` does not exist until T12A. What this route already establishes is
    the identity rule from section 10: one `agent_runs.id` is one application
    run, the server issues it, and a continuation after an approval interrupt
    will later run under this same id rather than minting a new one.
    """
    run = runs.create(settings.actor_id, body.user_message, settings.model_id)
    return RunCreatedResponse(run_id=run.id)


@app.get("/api/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: UUID) -> RunDetail:
    """Resolve a run id to its detail, or refuse identically for both failures.

    A malformed id is a 422 from path validation. A well-formed id that names no
    run, or names another actor's run, is the 403 that `runs.load` raises.
    """
    return runs.detail(run_id, settings.actor_id)


@app.post(
    "/api/runs/{run_id}/approvals/{tool_call_id}",
    response_model=RunDetail,
)
def post_approval_decision(
    run_id: UUID, tool_call_id: str, body: ApprovalDecisionRequest
) -> RunDetail:
    """Record the human decision against the server's own approval record.

    The browser supplies exactly one thing here: `approved` or `denied`. The run,
    the call id, the arguments, the hash, the preview, the expiry, and the
    current decision are all server owned, and `ApprovalDecisionRequest` forbids
    extra keys, so a payload attempting to carry any of them is a 422 before this
    body runs.

    **This route does not execute the tool.** It verifies, persists, and returns
    `RunDetail`, which is what section 9's response column specifies. The
    framework continuation is a separate `POST /api/agui` carrying `resume[]`,
    which reads the decision this route stored. See D-58.

    The validation order is ownership, then lifecycle, then approval row, fixed
    by D-55 and load bearing in both directions:

    1. `runs.load` refuses a run that does not exist or belongs to another actor,
       identically, so the response cannot enumerate real run ids.
    2. A run whose status forbids a decision is refused before the approval row
       is read at all, so a wrong-state request cannot discover whether a call id
       exists inside a run it does own.
    3. Only then is the row examined, in the order section 10's bridge lists:
       exists, unexpired, still pending.

    Step 3's checks are not the concurrency boundary. `runs.decide_approval`
    performs a guarded update, and a second request that lost the race is refused
    there even though its read observed a pending row.
    """
    run = runs.load(run_id, settings.actor_id)
    if run.status is not RunStatus.AWAITING_APPROVAL:
        # D-55's thirteenth code. Reached only after ownership resolved, so it
        # never distinguishes a missing run from a foreign one.
        raise RunStateInvalidError(
            f"run is {run.status.value} and cannot accept an approval decision"
        )

    approval = runs.load_approval(run_id, tool_call_id)
    if approval is None:
        # BUILD_SPEC proof 7's forgery case: a decision for a call the server
        # never deferred. Nothing was written, so nothing is decided.
        raise ApprovalNotFoundError()
    if approval.expires_at <= datetime.now(timezone.utc):
        raise ApprovalExpiredError()
    if approval.decision is not ApprovalState.PENDING:
        raise ApprovalAlreadyDecidedError()

    runs.decide_approval(run_id, tool_call_id, body.decision)
    return runs.detail(run_id, settings.actor_id)


@app.post("/api/agui")
async def post_agui(request: Request) -> Response:
    """The AG-UI transport. One application run per accepted user message.

    The handler lives in `agent.py` because the trust boundary it enforces is
    inseparable from how the adapter is constructed, and splitting the two across
    files would put half the argument in each. `agent.get_agent` is reached
    through the module attribute rather than a `from` import so the deterministic
    gate can substitute a model without a seam in this route.
    """
    return await agent.handle_agui_request(request)


@app.get("/api/linear/oauth/callback", response_class=PlainTextResponse)
def get_linear_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """The registered Linear OAuth redirect. T00W, under D-69 and D-70.

    The browser sees generic text and nothing else. No authorization code, state
    value, token, client secret, or provider response reaches the response body,
    and `no-store` keeps the page out of caches and out of history restoration. A
    browser landing here already holds an authorization code in its URL bar;
    there is no reason to put it in the page as well.

    `error`, or a missing `code` or `state`, is answered without contacting
    Linear. A user who declined consent has nothing to exchange, and calling the
    provider anyway would turn an ordinary cancellation into a failed token
    request.

    The work is delegated because the transaction phases are the correctness
    property here, and they belong beside the reasoning for them.
    """
    if error is not None or not code or not state:
        return _callback_response("Installation was not completed.", 400)

    try:
        linear_install.complete_installation(code, state)
    except linear_install.InstallationError as exc:
        return _callback_response(f"Installation failed. {exc}", 400)

    return _callback_response(
        "Trellis is installed. You can close this window and return to Linear.", 200
    )


def _callback_response(message: str, status: int) -> Response:
    return PlainTextResponse(
        message, status_code=status, headers={"Cache-Control": "no-store"}
    )


@app.post("/api/linear/webhook")
async def post_linear_webhook(request: Request) -> Response:
    """The registered Linear webhook. T00W, under D-69.

    Async because the raw body must be awaited, and because Linear allows five
    seconds. The trust decisions live in `linear_agent.py`; this route reads the
    exact bytes once, runs the CPU-only verification, and hands the durable
    decision to a thread so synchronous psycopg never blocks the event loop. That
    threadpool pattern is the one `agent.py` already uses for `runs.create_turn`.

    **The body is read exactly once, before anything else, and never
    re-serialized.** Declaring a Pydantic body parameter here would let FastAPI
    parse the payload before the signature is checked, and the bytes the
    signature covers would then be reconstructed rather than observed.

    **No outbound Linear call happens on this path.** Not to acknowledge, not to
    post an activity, not to revoke. A provider round trip inside a five second
    budget buys nothing the worker cannot do afterwards.

    The status codes are chosen for what they tell Linear to do next. A 401 or
    400 says this delivery will never be acceptable, so a retry would spend six
    hours reaching the same answer. A 200 says it is handled, including when the
    answer was a permanent refusal that is now durably recorded. A 5xx is
    reserved for our own failure before a durable decision exists, which is the
    one case where retrying can genuinely succeed.
    """
    raw_body = await request.body()

    try:
        linear_agent.verify_signature(raw_body, request.headers.get("Linear-Signature"))

        try:
            payload = json.loads(raw_body)
        except ValueError:
            raise linear_agent.WebhookRejected(400, "invalid_json") from None
        if not isinstance(payload, dict):
            raise linear_agent.WebhookRejected(400, "invalid_json")

        linear_agent.verify_freshness(payload)
        delivery_id = linear_agent.canonical_delivery_id(
            request.headers.get("Linear-Delivery")
        )
        body_sha256 = linear_agent.body_digest(raw_body)

        # Routing reads the signed body. `Linear-Event` carries the same idea in
        # a header the HMAC does not cover, so it stays diagnostic only.
        event_type = payload.get("type")
        action = payload.get("action")

        if event_type == linear_agent.TYPE_AGENT_SESSION:
            event = linear_agent.parse_agent_session_event(payload)
            result = await run_in_threadpool(
                linear_agent.accept_agent_session_event,
                delivery_id=delivery_id,
                body_sha256=body_sha256,
                payload=payload,
                event=event,
            )
            return JSONResponse(status_code=200, content=result)

        if (
            event_type == linear_agent.TYPE_OAUTH_APP
            and action == linear_agent.ACTION_REVOKED
        ):
            try:
                revocation = linear_agent.OAuthRevocationEvent.model_validate(payload)
            except ValidationError:
                raise linear_agent.WebhookRejected(
                    400, "unusable_revocation_payload"
                ) from None
            result = await run_in_threadpool(
                linear_agent.apply_oauth_revocation, revocation
            )
            return JSONResponse(status_code=200, content=result)

        # A signed event family T00W does not consume. Not stored, because an
        # event we do not handle is not work, and 200 rather than an error,
        # because Linear retrying it would change nothing.
        return JSONResponse(status_code=200, content={"disposition": "ignored"})

    except linear_agent.WebhookRejected as rejected:
        return JSONResponse(
            status_code=rejected.status, content={"error": {"code": rejected.reason}}
        )


@app.post(
    "/api/demo/reset",
    response_model=TasksResponse,
    dependencies=[Depends(_require_no_body)],
)
def post_demo_reset() -> TasksResponse:
    """Atomically replace all demo state with the fixed eleven-task fixture."""
    with pool.connection() as conn:
        tasks = seed.reset(settings.actor_id, conn=conn)
        conn.commit()
    return TasksResponse(tasks=tasks)
