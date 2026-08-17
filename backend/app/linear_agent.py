"""T00W ingress: the OAuth callback and the signed webhook boundary. D-69, D-70.

`main.py` owns the routes and this module owns every decision that matters. The
split matches how `agent.py` holds the AG-UI trust boundary rather than the route:
the reasoning about what a payload proves is inseparable from the code enforcing
it, and splitting them puts half the argument in each file.

**The webhook is a second untrusted ingress.** The browser was the first. A valid
Linear signature proves Linear sent the request. It does not prove this workspace
installed us, and it does not prove the human behind the event may operate
Trellis. Three separate facts, checked separately and in this order:

```text
1  HMAC-SHA256 over the exact raw bytes   did Linear send this
2  signed webhookTimestamp freshness      is it recent, not a replay
3  Linear-Delivery, UUID v4, unique       ordinary provider retry identity
4  sha256(raw body), unique               a forged delivery id buys nothing
5  installation binding                   is it for this installed app
6  human authorization                    may this person act as the actor
```

Steps 1 and 2 are the reason `Linear-Timestamp` is never consulted. The header is
not covered by the HMAC, so a value an attacker can edit cannot decide whether a
replay is fresh. The signed body's `webhookTimestamp` can only be changed by
breaking the signature, which is the property being relied on. `Linear-Event` is
likewise diagnostic only: routing reads the signed `type` and `action`.

**Nothing here calls Linear.** Not once, on any path. The webhook must answer
within five seconds, and an outbound call inside the request would put a provider
round trip inside that budget for no gain. Delivery of Agent Activities belongs to
the worker.

**Authorization never reads content.** Not `promptContext`, not `guidance`, not
issue text, comments, names, or emails. Those sit on the untrusted side of the
same boundary task titles do. Only provider-owned structured identity decides
whether a request may act.

**Provider envelopes use `extra="ignore"`**, the opposite of every model in
`models.py`, and they live here rather than there for that reason. `TrellisModel`
forbids extra keys because an undeclared key on the wire contract is attack
surface the server defines. A provider envelope is the other situation: Linear's
Agent APIs are Developer Preview and may add fields at any time, and refusing a
signed webhook because it grew a key we do not read would be brittleness rather
than strictness. Every field actually consumed is still type-checked, and the
complete authenticated payload is persisted regardless of what the models narrow.

That last point is deliberate and load bearing. `agentActivity.content`,
`signal`, and `signalMetadata` are stored exactly as received and are not parsed
here. Linear's prose documentation and its published schema describe the prompted
message differently, so any extractor written now would be a guess. The worker
resolves it against a real signed payload. A non-null `signal` is not ordinary
prompt text and must never be treated as such.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from psycopg.types.json import Json
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import sql
from .config import settings
from .db import pool


# Signed event families T00W consumes. Anything else is refused rather than
# stored, because an event family we do not handle is not work.
TYPE_AGENT_SESSION = "AgentSessionEvent"
TYPE_OAUTH_APP = "OAuthApp"

ACTION_CREATED = "created"
ACTION_PROMPTED = "prompted"
ACTION_REVOKED = "revoked"

AGENT_SESSION_ACTIONS = frozenset({ACTION_CREATED, ACTION_PROMPTED})

# Finite, machine-readable refusal reasons. Never prose, never user content, and
# never a provider payload. A refusal reason is durable state that an operator
# reads; making it a free-text field would turn the inbox into an uncontrolled
# copy of whatever arrived.
REFUSAL_INSTALLATION = "inactive_or_mismatched_installation"
REFUSAL_UNAUTHORIZED_HUMAN = "unauthorized_human"
REFUSAL_MISSING_CREATOR = "missing_creator"
REFUSAL_UNSUPPORTED_ACTION = "unsupported_agent_session_action"
REFUSAL_IDENTITY_MISMATCH = "provider_identity_mismatch"

STATUS_PENDING = "pending"
STATUS_REFUSED = "refused"


class WebhookRejected(Exception):
    """The request is refused before any durable work exists.

    Carries the HTTP status the route returns. The distinction this class draws
    is the one that decides whether Linear retries: a 401 or 400 tells the
    provider this delivery is not acceptable and will never be, while a 5xx
    invites the retry that a transient local failure deserves.

    Deliberately not a `PolicyError`. BUILD_SPEC section 6's fourteen codes are
    the application's rejection vocabulary for its own clients, and a webhook
    that fails signature verification is not making an application request. D-69
    declines to grow that table for transport.
    """

    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"{reason} ({status})")


class _AgentSessionBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    organizationId: str = Field(min_length=1)  # noqa: N815 - provider field name
    appUserId: str | None = None  # noqa: N815 - provider field name
    creatorId: str | None = None  # noqa: N815 - provider field name


class _AgentActivityBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    agentSessionId: str = Field(min_length=1)  # noqa: N815 - provider field name
    userId: str = Field(min_length=1)  # noqa: N815 - provider field name
    # Kept whole and unparsed. See the module docstring.
    content: Any = None
    signal: str | None = None
    signalMetadata: Any = None  # noqa: N815 - provider field name


class AgentSessionEvent(BaseModel):
    """The narrowed view of a signed AgentSessionEvent.

    Only what ingress consumes. The complete payload is persisted separately, so
    narrowing here discards nothing durable.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    action: str = Field(min_length=1)
    organizationId: str = Field(min_length=1)  # noqa: N815 - provider field name
    oauthClientId: str = Field(min_length=1)  # noqa: N815 - provider field name
    appUserId: str = Field(min_length=1)  # noqa: N815 - provider field name
    agentSession: _AgentSessionBody  # noqa: N815 - provider field name
    agentActivity: _AgentActivityBody | None = None  # noqa: N815


class OAuthRevocationEvent(BaseModel):
    """The narrowed view of a signed OAuthApp revocation."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    action: str = Field(min_length=1)
    organizationId: str = Field(min_length=1)  # noqa: N815 - provider field name
    oauthClientId: str = Field(min_length=1)  # noqa: N815 - provider field name
    createdAt: datetime  # noqa: N815 - provider field name


# --------------------------------------------------------------- verification


def verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    """HMAC-SHA256 over the exact bytes received. Raises on any failure.

    `raw_body` must be what arrived, never a re-serialization. Round-tripping
    JSON reorders keys and normalizes whitespace, and the signature covers the
    original bytes, so a parse-then-dump would fail verification for a legitimate
    request and, worse, could be "fixed" later by someone relaxing the check.

    The length and hex checks come before `compare_digest` because that function
    requires equal-length inputs to be meaningful, and because a malformed header
    should be refused as malformed rather than compared.
    """
    secret = settings.linear_webhook_secret
    if not secret:
        # A misconfigured receiver must not silently accept anything. This is the
        # one path here that is a server fault rather than a client one.
        raise WebhookRejected(500, "webhook_secret_not_configured")

    if not signature_header or len(signature_header) != 64:
        raise WebhookRejected(401, "malformed_signature")
    try:
        provided = bytes.fromhex(signature_header)
    except ValueError:
        raise WebhookRejected(401, "malformed_signature") from None

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    if not hmac.compare_digest(provided, expected):
        raise WebhookRejected(401, "invalid_signature")


def verify_freshness(payload: dict, *, now_ms: float | None = None) -> None:
    """Replay protection, from the signed body and nothing else.

    `webhookTimestamp` lives inside the HMAC-covered body, so changing it breaks
    the signature. The `Linear-Timestamp` header carries the same information and
    is not covered, which is exactly why it is not read here.

    `bool` is excluded explicitly because it is a subclass of `int` in Python, so
    a payload carrying `true` would otherwise pass an `isinstance(value, int)`
    check and be treated as the epoch. The future direction is bounded as well as
    the past: a far-future timestamp would otherwise stay "fresh" indefinitely.
    """
    value = payload.get("webhookTimestamp")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WebhookRejected(401, "missing_timestamp")
    if not math.isfinite(value):
        raise WebhookRejected(401, "invalid_timestamp")

    current = now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000
    if abs(current - float(value)) > settings.linear_webhook_freshness_seconds * 1000:
        raise WebhookRejected(401, "stale_timestamp")


def canonical_delivery_id(header: str | None) -> str:
    """The provider's delivery identity, validated and canonicalized.

    `UUID(header, version=4)` is not validation: that form *coerces* the input by
    rewriting the version and variant bits, so a v1 UUID would be silently
    accepted as a v4. Parsing first and checking `.version` afterwards is the
    check the coercing form only appears to perform.

    Canonical `str(UUID)` is stored so that case and brace variations of the same
    delivery cannot occupy two rows and defeat the unique constraint.
    """
    if not header:
        raise WebhookRejected(400, "missing_delivery_id")
    try:
        delivery = UUID(header)
    except ValueError:
        raise WebhookRejected(400, "malformed_delivery_id") from None
    if delivery.version != 4:
        raise WebhookRejected(400, "malformed_delivery_id")
    return str(delivery)


def body_digest(raw_body: bytes) -> str:
    """The second dedupe identity. See D-69.

    The HMAC authenticates the body and not the headers, so an identical signed
    body replayed under a fresh `Linear-Delivery` would pass every cryptographic
    check and, without this, buy a second unit of work.
    """
    return hashlib.sha256(raw_body).hexdigest()


# ------------------------------------------------------------- authorization


def _assert_internally_consistent(event: AgentSessionEvent) -> None:
    """The outer envelope and the nested objects must agree.

    Cheap, and it closes a gap the installation match alone would not. The
    installation is located using the outer identifiers, so if the nested session
    named a different organization the event could be authorized against one
    workspace while describing another.
    """
    session = event.agentSession
    if session.organizationId != event.organizationId:
        raise _Refusal(REFUSAL_IDENTITY_MISMATCH)
    if session.appUserId is not None and session.appUserId != event.appUserId:
        raise _Refusal(REFUSAL_IDENTITY_MISMATCH)
    if event.agentActivity is not None and event.agentActivity.agentSessionId != session.id:
        raise _Refusal(REFUSAL_IDENTITY_MISMATCH)


class _Refusal(Exception):
    """A well-formed event whose application answer is permanently negative.

    Distinct from `WebhookRejected` because the outcomes differ: this produces a
    durable refused row and a 200, since asking Linear to retry an event we will
    always refuse wastes six hours of provider retries to reach the same answer.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _authorize(event: AgentSessionEvent, installation: dict | None) -> None:
    """Installation binding and human authorization. Raises `_Refusal`.

    Human authorization is action-specific, and conflating the two actions would
    be a real hole. A `created` event is authorized by who opened the session; a
    `prompted` event is authorized by who wrote *that message*. Allowing a prompt
    to inherit the creator's authorization would let anyone in the workspace type
    into a session the allowed human happened to open.
    """
    if installation is None:
        raise _Refusal(REFUSAL_INSTALLATION)

    if event.action not in AGENT_SESSION_ACTIONS:
        raise _Refusal(REFUSAL_UNSUPPORTED_ACTION)

    allowed = installation["allowed_linear_user_id"]

    if event.action == ACTION_CREATED:
        creator = event.agentSession.creatorId
        if creator is None:
            # Nullable in Linear's schema. A session with no responsible human
            # cannot be authorized under a single-human policy, and defaulting it
            # to the allowed user would invent consent.
            raise _Refusal(REFUSAL_MISSING_CREATOR)
        if creator != allowed:
            raise _Refusal(REFUSAL_UNAUTHORIZED_HUMAN)
        return

    activity = event.agentActivity
    if activity is None:
        raise _Refusal(REFUSAL_IDENTITY_MISMATCH)
    if activity.userId != allowed:
        raise _Refusal(REFUSAL_UNAUTHORIZED_HUMAN)


# ------------------------------------------------------------------ ingress


def parse_agent_session_event(payload: dict) -> AgentSessionEvent:
    """Narrow a signed payload, or refuse it as structurally unusable.

    A 400 rather than a durable refused row, because the inbox requires an
    organization and a session id to store a row at all. Contorting a payload
    that lacks them into a "refused AgentSession" would mean inventing the
    identity the refusal is supposed to be about.
    """
    try:
        return AgentSessionEvent.model_validate(payload)
    except ValidationError:
        raise WebhookRejected(400, "unusable_agent_session_payload") from None


def accept_agent_session_event(
    *,
    delivery_id: str,
    body_sha256: str,
    payload: dict,
    event: AgentSessionEvent,
) -> dict:
    """Decide, persist, and commit before the caller answers. Synchronous.

    The order is the contract:

    1. Prove the provider's own identifiers agree with each other.
    2. Locate the ACTIVE installation the three identifiers name.
    3. Authorize the human for this specific action.
    4. Insert pending or refused, and commit.

    Steps 1 through 3 raise `_Refusal` rather than returning, and every refusal
    still produces a durable row. That is the point: a permanent refusal is an
    answer, and recording it means a redelivery of the same event is recognized
    as already handled rather than re-evaluated.

    The complete authenticated payload is stored, not the narrowed model. What
    ingress ignores today the worker may need tomorrow, and it arrived signed.
    """
    with pool.connection() as conn:
        installation = conn.execute(
            sql.SELECT_LINEAR_INSTALLATION_FOR_EVENT,
            {
                "organization_id": event.organizationId,
                "oauth_client_id": event.oauthClientId,
                "app_user_id": event.appUserId,
            },
        ).fetchone()

        try:
            _assert_internally_consistent(event)
            _authorize(event, installation)
        except _Refusal as refusal:
            status, reason = STATUS_REFUSED, refusal.reason
        else:
            status, reason = STATUS_PENDING, None

        row = conn.execute(
            sql.INSERT_LINEAR_INBOX,
            {
                "delivery_id": delivery_id,
                "body_sha256": body_sha256,
                "organization_id": event.organizationId,
                "agent_session_id": event.agentSession.id,
                "action": event.action,
                "payload": Json(payload),
                "status": status,
                "refusal_reason": reason,
            },
        ).fetchone()

        if row is None:
            outcome = _classify_conflict(conn, delivery_id, body_sha256)
        else:
            outcome = {"disposition": status, "refusal_reason": reason, "duplicate": False}

        # The commit is the acknowledgement. Nothing above this line is durable,
        # and returning 200 before it would let a restart lose accepted work
        # Linear believes was delivered.
        conn.commit()

    return outcome


def _classify_conflict(conn, delivery_id: str, body_sha256: str) -> dict:
    """Why the insert conflicted, read from both unique identities.

    Four cases, and the two identities are read separately because the
    interesting ones are where they disagree:

    ```text
    same delivery, same body      ordinary duplicate
    new delivery, same body       identical signed body, changed unsigned header
    same delivery, new body       provider identity conflict
    delivery hits A, body hits B  provider identity conflict
    ```

    None of the four is called an attack. A duplicate is ordinary provider retry
    traffic, and the conflicting cases are surprising rather than proven
    hostile: the honest report is that two identities disagreed. No payload is
    logged in any of them.

    Every case produces no second unit of work, which is the property that
    matters, and every case answers 200, because asking Linear to retry a
    delivery already recorded would produce this same answer an hour later.
    """
    by_delivery = conn.execute(
        sql.SELECT_LINEAR_INBOX_BY_DELIVERY, {"delivery_id": delivery_id}
    ).fetchone()
    by_body = conn.execute(
        sql.SELECT_LINEAR_INBOX_BY_BODY, {"body_sha256": body_sha256}
    ).fetchone()

    if by_delivery is not None and by_body is not None and by_delivery["id"] == by_body["id"]:
        return {"disposition": "duplicate", "duplicate": True, "conflict": None}
    if by_delivery is None and by_body is not None:
        # Same signed body under a different delivery header. The defense
        # in depth case D-69 added the body digest for.
        return {"disposition": "duplicate", "duplicate": True, "conflict": "body_replay"}
    return {
        "disposition": "duplicate",
        "duplicate": True,
        "conflict": "provider_identity_conflict",
    }


def apply_oauth_revocation(event: OAuthRevocationEvent) -> dict:
    """Revoke locally, guarded by when the revocation actually happened.

    Not routed through the inbox. The inbox is AgentSession work and requires a
    session id this event does not have, and revocation is control-plane state
    rather than something a worker should process later. Forcing it through a
    queue would also mean an installation stayed usable until a worker got to it.

    **This does not call Linear's revoke endpoint.** Linear is telling us the
    authorization is already gone; calling back to revoke it would be answering a
    notification with a request about the thing it just notified us of.

    Idempotent by construction. A second delivery matches no ACTIVE row and
    reports zero, which is a 200 rather than an error, because the desired state
    already holds.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.REVOKE_LINEAR_INSTALLATION,
            {
                "organization_id": event.organizationId,
                "oauth_client_id": event.oauthClientId,
                "revocation_created_at": event.createdAt,
            },
        ).fetchone()
        conn.commit()
    return {"revoked": row is not None}
