"""D-74. The absolute resource ceilings, and the transport admission boundary.

Every value here is a safety invariant, not an operational knob, which is why
none of them reads from the environment the way `config.Settings` does. The
values in `Settings` are policy: a blast radius threshold or an approval TTL is
a product decision an operator may reasonably tune per deployment. These are the
other thing entirely. A ceiling an operator can raise from 256 KiB to 10 GiB by
exporting a variable is not a ceiling, and the failure it is supposed to prevent
would then be one misconfigured deployment away rather than impossible.

If a deployment ever needs to be stricter than the code, the safe shape is a
configured value that may only *lower* a code-owned cap, never exceed it.
Nothing in this build needs that yet, so it is not built yet.

Two ceilings live here and they protect different resources:

    transport bytes   how much of a request body may ever be buffered
    typed size        how much accepted content a validated field may carry

They are not substitutes. Capping `CreateRunRequest.user_message` at 8,000
characters says nothing about how many megabytes the server buffered before
Pydantic ever saw the field. The byte ceiling is what makes that bounded, and it
is enforced before parsing rather than during it.

The Linear webhook carries its own, larger ceiling. Trellis controls the shape
of its own browser requests; it does not control what Linear assembles into
`promptContext`, which the provider builds from the issue, the surrounding
comments, and its own guidance. Naming that asymmetry as a second constant is
honest in a way that quietly raising every route to the larger number would not
be.
"""

import json


# --------------------------------------------------------------- transport

# The ceiling for every body-bearing Trellis route. Generous against observed
# traffic: the largest payload anywhere in the repository is the 3,695 byte
# Linear contract fixture, so this is roughly seventy times the largest body
# this application has been seen to handle.
DEFAULT_MAX_BODY_BYTES = 256 * 1024

# `POST /api/linear/webhook` only. See the module docstring for why this is a
# separate named constant rather than a larger `DEFAULT_MAX_BODY_BYTES`.
LINEAR_WEBHOOK_MAX_BODY_BYTES = 1024 * 1024

LINEAR_WEBHOOK_PATH = "/api/linear/webhook"

# The single rejection message. Kept here rather than inline so a test can pin
# the exact string that distinguishes a transport refusal from every other 422.
BODY_TOO_LARGE_MESSAGE = "request body is too large"


# ------------------------------------------------------------- accepted text

# The browser paths: `POST /api/runs` and the accepted newest AG-UI message.
BROWSER_USER_MESSAGE_MAX_CHARS = 8_000

# Linear's two AgentSession actions carry materially different inputs, and
# collapsing them into one ceiling would be wrong in both directions. A
# `prompted` event is one human message and belongs at the browser ceiling. A
# `created` event carries `promptContext`, which the provider assembles from an
# issue and its comment thread, and which is legitimately much longer.
LINEAR_PROMPTED_MESSAGE_MAX_CHARS = 8_000
LINEAR_CREATED_CONTEXT_MAX_CHARS = 64_000


# ------------------------------------------------------ typed tool arguments

TASK_NOTES_MAX_CHARS = 10_000

# One accepted call may not name more than this many targets. It does not bound
# how many such calls a run may make; that is a separate dimension and a later
# task's problem.
BULK_TASK_IDS_MAX = 50
DELETE_TASK_IDS_MAX = 50

PLAN_SUMMARY_MAX_CHARS = 2_000
PLAN_STEPS_MAX = 25
PLAN_STEP_MAX_CHARS = 2_000


# ----------------------------------------------------------------- admission

# Methods whose requests can carry a body. A GET is passed through untouched
# rather than awaited, so admission cannot introduce a stall on a request shape
# that has nothing to admit.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_REJECTION = json.dumps(
    {"error": {"code": "VALIDATION_ERROR", "message": BODY_TOO_LARGE_MESSAGE}}
).encode("utf-8")


def body_limit_for(path: str) -> int:
    """The absolute byte ceiling that governs one request path."""
    return (
        LINEAR_WEBHOOK_MAX_BODY_BYTES
        if path == LINEAR_WEBHOOK_PATH
        else DEFAULT_MAX_BODY_BYTES
    )


class BodySizeLimitMiddleware:
    """Pure ASGI admission: bound the body, then replay it byte for byte.

    Why this shape rather than the obvious one. The obvious implementation
    wraps `receive` and raises once the running total passes the ceiling. That
    works for a route reading the body itself, and it fails for a route with a
    declared Pydantic body: FastAPI catches whatever is raised while it is
    parsing and answers `400 {"detail": ...}`, which is outside the closed error
    vocabulary `errors.py` owns. The rejection would then depend on which kind
    of route happened to receive it.

    So admission finishes before the application is called at all. The stream is
    consumed up to the ceiling plus one byte, and one of two things happens:

    - over the ceiling, this middleware answers the section 6 envelope itself
      and the application is never invoked, so no route, parser, handler,
      signature check, database transaction, or model call observes the request;
    - at or under it, the exact bytes are replayed downstream as a single
      `http.request` and everything proceeds as though nothing intervened.

    **The replay is byte for byte.** Linear signs the raw request body, so a
    boundary that parsed and re-serialized on the way through would break every
    signature; `test_admitted_bytes_reach_signature_verification_unchanged`
    pins that. Nothing here decodes, parses, strips, or normalizes.

    `Content-Length` is used only as an early exit, never as the authority. A
    client that understates it, or omits it entirely by chunking, is bounded by
    the same running count of bytes actually received.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method", "").upper() not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        limit = body_limit_for(scope.get("path", ""))

        if _declared_length(scope) > limit:
            await _reject(send)
            return

        chunks: list[bytes] = []
        total = 0
        disconnected = False

        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue

            chunk = message.get("body", b"")
            total += len(chunk)
            if total > limit:
                # Nothing beyond limit + 1 bytes is ever held, and the
                # application has not been called, so there is nothing to
                # unwind.
                await _reject(send)
                return
            chunks.append(chunk)

            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if disconnected:
                return {"type": "http.disconnect"}
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            # The body was delivered in one message. Anything the application
            # asks for afterwards is a disconnect, and must come from the real
            # transport rather than be synthesized here.
            return await receive()

        await self.app(scope, replay, send)


def _declared_length(scope) -> int:
    """`Content-Length` if the client sent a usable one, else zero.

    Zero, not infinity: an absent or unparseable header must fall through to
    the streamed count rather than short circuit the request in either
    direction.
    """
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


async def _reject(send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 422,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_REJECTION)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _REJECTION})
