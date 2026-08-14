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

Section 9's table lists seven endpoints. Four are here. The rest belong to the
tasks that own their behavior, under D-44: `/api/demo/reset` to T09 with the
fixture, `/api/agui` to T12A, the approvals decision to T12B with the approval
bridge, and undo to T18. `/api/runs/{id}/resume` is not here and is not coming,
because D-36 credited resume and orphan sweep as removed in full and spent the
0.25d; section 9's table is corrected to match.

Actor identity is `settings.actor_id`. This build has one actor by design, and
authentication is out of scope for the demo, but every authorization decision
still flows through the actor rather than around it, so introducing real
identity later is a change of where `actor_id` comes from and nothing else.
"""

from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import domain, runs, seed
from app.config import settings
from app.db import pool
from app.errors import PolicyError, ValidationFailedError
from app.models import (
    CreateRunRequest,
    ListTasksArgs,
    RunCreatedResponse,
    RunDetail,
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
