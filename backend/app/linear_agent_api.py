"""The sole remote Linear provider boundary, from T00W. See D-69.

**This is the only file in shipped application code permitted to contain a
Linear provider endpoint literal**, and the `T00L Linear boundary` CI gate
asserts exactly that. `linear_install.py` and `linear_agent_worker.py` reach
Linear through the functions below and open no HTTP client of their own.

The rule is structural rather than stylistic. Three modules each holding their
own endpoint and their own client is three places for a timeout policy to drift,
three places a credential can be logged, and three places a later change can
quietly add a call nobody reviewed. Concentrating it here means the question
"what can this application say to Linear" is answered by reading one file.

D-69 authorizes exactly four capabilities here and no others:

```text
OAuth authorization URL construction   linear.app/oauth/authorize
OAuth token exchange, refresh, revoke  api.linear.app/oauth/*
Installation identity, read only       viewer { id }
AgentActivity operations               agentActivityCreate
```

The four Linear issue mutations are absent and must stay absent. T00W talks to
Linear; it does not touch a Linear issue. Issue projection is T26 through T29 and
remains deferred. The same CI gate that exempts this file from the endpoint rule
still forbids those four mutation names inside it, which is why they are
described here rather than spelled: the gate is deliberately blunt enough that
even a comment naming them is a failure, and weakening it so documentation could
name them would be weakening it for code too.

**Secrets never reach an exception, a log line, or a response.** Every failure
below raises `LinearApiError` carrying an operation name and a status code.
Bodies are not interpolated into messages, because a Linear error body can echo
the request, and the request carries a client secret on the token endpoints and a
bearer token everywhere else. `_post_graphql` therefore reports GraphQL failures
by message text extracted from the documented error shape rather than by dumping
the payload.

This module holds no database state and makes no policy decision. It does not
know which installation is active, whether a token is expired, or whether a human
is authorized. Callers own all of that. That separation is what lets the
deterministic tests drive every branch here against a transport stub without a
credential, a network, or a live workspace.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import settings


# The four authorized endpoints. Named constants rather than inline literals so
# the CI gate has a single obvious place to look and a reader can see the whole
# provider surface at once.
LINEAR_AUTHORIZE_URL = "https://linear.app/oauth/authorize"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"
LINEAR_REVOKE_URL = "https://api.linear.app/oauth/revoke"
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# D-69. `actor=app` installs as the application itself rather than as the
# installing human, which is what makes Trellis a native agent in the workspace
# rather than a script acting with someone's identity.
LINEAR_ACTOR = "app"

# Only what T00W needs. `app:mentionable` and `app:assignable` are what let the
# agent be mentioned and delegated to; `read` and `write` cover the
# installation identity query and Agent Activity creation. No issue scope beyond
# `write` is requested, and nothing here uses it for issues.
LINEAR_SCOPES = ("read", "write", "app:mentionable", "app:assignable")

# D-70. Both identifiers, in one round trip. `User.organization` is non-null in
# Linear's published schema. This is installation identity, not workspace
# resolution: it asks who the just-issued token belongs to and nothing else.
_GRAPHQL_IDENTITY = "query InstallationIdentity { viewer { id organization { id } } }"
_GRAPHQL_CREATE_ACTIVITY = """
mutation CreateAgentActivity($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) {
    success
    agentActivity { id }
  }
}
"""


class _Organization(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)


class _Viewer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    organization: _Organization


class InstallationIdentity(BaseModel):
    """The validated answer to "who am I, and where". See D-70."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    app_user_id: str = Field(min_length=1)
    organization_id: str = Field(min_length=1)


class _AgentActivityIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)


class AgentActivityResult(BaseModel):
    """The validated `agentActivityCreate` payload.

    Both fields are required. `success` alone is not enough: a payload claiming
    success while carrying no activity is a provider contract violation, and
    accepting it would let the worker record a delivery that may not exist.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    success: bool
    agentActivity: _AgentActivityIdentity  # noqa: N815 - provider field name

    @field_validator("success")
    @classmethod
    def _must_be_true(cls, value: bool) -> bool:
        if not value:
            raise ValueError("provider reported success=false")
        return value


class LinearApiError(RuntimeError):
    """A remote Linear call failed.

    Deliberately not a `PolicyError` and deliberately not carrying one of the
    fourteen error codes. BUILD_SPEC section 6's table is the application's
    rejection vocabulary, and a provider being unreachable is not an application
    rejection. D-69 declines to grow the kernel taxonomy for transport failure.
    """

    def __init__(self, operation: str, status: int | None, detail: str) -> None:
        self.operation = operation
        self.status = status
        self.detail = detail
        super().__init__(f"linear {operation} failed (status={status}): {detail}")


class LinearTokens(BaseModel):
    """One validated token response. Identical shape for exchange and refresh.

    A Pydantic model rather than a dataclass, and that is not decoration. These
    values are written to `linear_installations`, so the boundary is the last
    place a wrong type can be stopped before it reaches a column. A dataclass
    annotated `scope: str` would happily carry a list, because annotations are
    not checked at runtime, and the failure would surface as bad data rather
    than as a rejected response.

    `extra="ignore"` rather than `forbid`, which is the opposite of the rule
    every request model in `models.py` follows, and deliberately so. Those models
    guard an inbound trust boundary where an unexpected key is an attack surface.
    This is a provider response: Linear may add a field at any time, and refusing
    to install because a response grew a key we do not read would be brittleness
    rather than strictness. Unknown keys are ignored; known keys are type-checked.

    `refresh_token` is optional because Linear omits it on client-credentials
    style responses. When present it replaces the token used to obtain it, since
    Linear rotates refresh tokens, so the caller must persist what came back
    rather than assume the old value survives.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    access_token: str = Field(min_length=1)
    refresh_token: str | None = None
    expires_in: int = Field(gt=0)
    token_type: str = Field(min_length=1)

    # Linear documents `scope` as a string today, and as an array only for
    # applications created before 1 December 2023. The Trellis application is
    # created now, so the legacy shape cannot occur for it, and this fails closed
    # on anything that is not the documented string rather than guessing.
    #
    # Failing closed is the right direction here because the alternative is
    # silently coercing a list into a text column, where the damage is discovered
    # later and by something else. A refused install reports its own cause.
    scope: str


def new_oauth_state() -> str:
    """A fresh, unguessable OAuth state value.

    `token_urlsafe` rather than `uuid4` because this is a security parameter,
    not an identifier: it needs entropy from a CSPRNG, and a UUID's version and
    variant bits are structure an attacker does not have to guess.

    The caller stores only a hash of this. See `linear_install.py`.
    """
    return secrets.token_urlsafe(32)


def authorization_url(state: str) -> str:
    """The URL the operator opens to install Trellis into a workspace.

    `redirect_uri` is derived from one configured origin rather than configured
    separately, so it cannot drift from the webhook URL's hostname. It must match
    what is registered in the Linear application exactly, including scheme.
    """
    query = urlencode(
        {
            "client_id": settings.linear_client_id,
            "redirect_uri": settings.linear_oauth_redirect_url,
            "response_type": "code",
            "scope": ",".join(LINEAR_SCOPES),
            "state": state,
            "actor": LINEAR_ACTOR,
        }
    )
    return f"{LINEAR_AUTHORIZE_URL}?{query}"


def exchange_code(code: str, *, client: httpx.Client | None = None) -> LinearTokens:
    """Trade an authorization code for tokens.

    The code is single use and short lived at the provider, so a failure here is
    terminal for that install attempt rather than retryable. The caller has
    already consumed its own state row by this point, which is deliberate: a
    failed exchange must not leave a state value that a second attempt could
    reuse.
    """
    return _post_token(
        "oauth_exchange",
        {
            "code": code,
            "redirect_uri": settings.linear_oauth_redirect_url,
            "client_id": settings.linear_client_id,
            "client_secret": settings.linear_client_secret,
            "grant_type": "authorization_code",
        },
        client=client,
    )


def refresh_tokens(
    refresh_token: str, *, client: httpx.Client | None = None
) -> LinearTokens:
    """Exchange a refresh token for a new pair.

    Linear rotates refresh tokens, so the response's `refresh_token` replaces the
    one passed in and the old value must be considered spent. Linear documents a
    grace period during which a failed refresh can be replayed, which makes a
    lost race recoverable, but it is recovery tolerance and not a substitute for
    the caller's lock. `linear_agent_worker.py` owns that lock; this function is
    unaware of concurrency by design.
    """
    return _post_token(
        "oauth_refresh",
        {
            "refresh_token": refresh_token,
            "client_id": settings.linear_client_id,
            "client_secret": settings.linear_client_secret,
            "grant_type": "refresh_token",
        },
        client=client,
    )


def revoke_token(
    token: str,
    *,
    token_type_hint: str | None = None,
    client: httpx.Client | None = None,
) -> None:
    """Revoke one token at the provider, in the documented modern form. D-70.

    The request carries `token` and an optional `token_type_hint`, and nothing
    else. An earlier version of this function also sent the token as a bearer
    header, which is not the documented request; the risk of that is not a noisy
    failure but a quiet one, where the endpoint accepts the call and the caller
    believes a credential was revoked when it was not.

    One token per call, so a caller cleaning up a failed installation can attempt
    the refresh token and the access token independently and have the second
    attempt survive the first failing.

    A provider-side failure raises, and the caller decides whether local
    revocation proceeds anyway. It usually should: an installation we can no
    longer use is revoked locally whether or not the remote call succeeded, and
    leaving it `active` because a network call failed is the less safe direction.
    """
    form = {"token": token}
    if token_type_hint is not None:
        form["token_type_hint"] = token_type_hint
    response = _request(
        "oauth_revoke", "POST", LINEAR_REVOKE_URL, client=client, data=form
    )
    if response.status_code != 200:
        raise LinearApiError("oauth_revoke", response.status_code, "revocation refused")


def granted_scope_set(tokens: LinearTokens) -> set[str]:
    """The granted scopes as a set. D-70.

    Scope is comma-separated in the authorization URL and space-delimited in the
    token response. Those are different formats in different directions, and code
    assuming the request format round-trips is wrong in a way that surfaces only
    against the live provider. Splitting on whitespace and comparing sets removes
    both the format and the ordering from the comparison.
    """
    return set(tokens.scope.split())


def has_bearer_token_type(tokens: LinearTokens) -> bool:
    """Bearer, compared case-insensitively.

    The provider documents `Bearer`. Treating that capitalization as part of the
    contract would be inventing a requirement the provider never stated.
    """
    return tokens.token_type.casefold() == "bearer"


def fetch_installation_identity(
    access_token: str, *, client: httpx.Client | None = None
) -> InstallationIdentity:
    """Who this token is, and which workspace it belongs to. See D-70.

    Both values are authorization inputs rather than bookkeeping. An
    `AgentSessionEvent` is bound to an installation by matching `organizationId`,
    `oauthClientId`, and `appUserId` together, so an installation missing either
    identifier cannot authorize a webhook at all.

    Neither can be obtained anywhere else honestly. The organization is not in the
    redirect or the token response; taking it from configuration would let a
    mistyped variable bind the installation to a workspace that never installed
    us, and learning it from the first webhook would mean trusting a webhook
    before knowing which workspace it should have come from.

    Read only, and the only non-activity GraphQL operation authorized. It performs
    no workspace search and accepts no workspace name, which is what keeps it
    distinct from the T26 resolution that remains deferred.
    """
    data = _post_graphql(
        "installation_identity", access_token, _GRAPHQL_IDENTITY, {}, client=client
    )
    try:
        viewer = _Viewer.model_validate(data.get("viewer"))
    except ValidationError:
        raise LinearApiError(
            "installation_identity", None, "response carried no viewer identity"
        ) from None
    return InstallationIdentity(
        app_user_id=viewer.id, organization_id=viewer.organization.id
    )


def create_agent_activity(
    access_token: str,
    *,
    agent_session_id: str,
    activity_id: str,
    content: dict,
    client: httpx.Client | None = None,
) -> None:
    """Post one Agent Activity into a Linear session.

    `activity_id` is supplied by the caller rather than generated here, and that
    is the whole point of the parameter. `AgentActivityCreateInput` accepts a
    UUID v4 and generates one when omitted, so the caller persists an id before
    sending and reuses it on retry, which is the strongest duplicate suppression
    available without a proven provider contract.

    **This does not make delivery exactly-once, and nothing in T00W claims it
    does.** Whether resubmitting the same id is a safe no-op, an error, or a
    second activity is unverified: the probe needs a live workspace token. Until
    it runs, delivery is documented as at-least-once with local suppression, and
    the crash window between Linear accepting an activity and the worker
    recording that locally is a known gap rather than a solved problem.
    """
    variables = {
        "input": {
            "id": activity_id,
            "agentSessionId": agent_session_id,
            "content": content,
        }
    }
    data = _post_graphql(
        "agent_activity_create",
        access_token,
        _GRAPHQL_CREATE_ACTIVITY,
        variables,
        client=client,
    )
    try:
        AgentActivityResult.model_validate(data.get("agentActivityCreate"))
    except ValidationError:
        raise LinearApiError(
            "agent_activity_create",
            None,
            "provider did not confirm the activity was created",
        ) from None


def _post_token(
    operation: str, form: dict, *, client: httpx.Client | None
) -> LinearTokens:
    """POST the OAuth token endpoint and parse the documented response shape.

    The form carries `client_secret`. Nothing derived from `form` or from the
    response body is placed in an exception message, which is why the failure
    path below reports a status and a fixed string rather than the payload.
    """
    response = _request(operation, "POST", LINEAR_TOKEN_URL, client=client, data=form)
    if response.status_code != 200:
        raise LinearApiError(operation, response.status_code, "token endpoint refused")

    try:
        body = response.json()
    except ValueError:
        raise LinearApiError(operation, response.status_code, "response was not JSON") from None

    try:
        return LinearTokens.model_validate(body)
    except ValidationError as exc:
        # Only the field names and error types, never the values. A validation
        # error rendered in full repeats the input, and the input here is a token
        # response. `error_count` and the located field names are enough to
        # diagnose a shape change without printing a credential.
        fields = sorted(
            {".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()}
        )
        detail = f"response failed validation at: {', '.join(fields)}" if fields else (
            "response failed validation"
        )
        raise LinearApiError(operation, response.status_code, detail) from None


def _post_graphql(
    operation: str,
    access_token: str,
    query: str,
    variables: dict,
    *,
    client: httpx.Client | None,
) -> dict:
    """POST a GraphQL document and return `data`, or raise.

    GraphQL's failure mode is the reason this wrapper exists. A Linear GraphQL
    error arrives as HTTP 200 with an `errors` array, so a caller checking only
    the status code would treat a refusal as a success and read `data` that is
    null. Both are checked here, in that order.

    Only the documented `message` field of the first error is surfaced. The rest
    of the error object can contain the offending query and variables, and the
    variables of a token-bearing request are not something to put in a log.
    """
    response = _request(
        operation,
        "POST",
        LINEAR_GRAPHQL_URL,
        client=client,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if response.status_code != 200:
        raise LinearApiError(operation, response.status_code, "graphql request refused")

    try:
        body = response.json()
    except ValueError:
        raise LinearApiError(operation, response.status_code, "response was not JSON") from None

    errors = body.get("errors")
    if errors:
        message = "graphql error"
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            candidate = errors[0].get("message")
            if isinstance(candidate, str) and candidate:
                message = candidate
        raise LinearApiError(operation, response.status_code, message)

    data = body.get("data")
    if not isinstance(data, dict):
        raise LinearApiError(operation, response.status_code, "response carried no data")
    return data


def _request(
    operation: str,
    method: str,
    url: str,
    *,
    client: httpx.Client | None,
    **kwargs,
) -> httpx.Response:
    """Every outbound Linear request passes through here.

    One place for the timeout, one place for transport-failure translation, and
    one seam for the deterministic tests. The `client` parameter is an injection
    point rather than a configuration option: passing a client backed by
    `httpx.MockTransport` drives every branch above without a credential or a
    network, which is what keeps the T00W gate offline.

    A transport failure is reported with `status=None` so callers can distinguish
    "Linear said no" from "Linear was unreachable". Those are different problems
    and only the caller knows whether either is safe to repeat.

    **Exactly one outbound attempt per call, and no retry lives here.** The
    transport is constructed with `retries=0` explicitly rather than relying on
    that being httpx's default, because a default is a thing a dependency upgrade
    can change and this one is load bearing.

    The reason is `agentActivityCreate`, which is a remote side effect. A
    connection that dies after the request was transmitted leaves two states this
    code cannot tell apart: Linear never received it, or Linear committed the
    activity and the response was lost. Retrying resolves that ambiguity by
    guessing, and the guess is only safe if replaying the same activity UUID is
    proven idempotent at the provider. That probe needs a live workspace and has
    not run. Until it does, one call issues one mutation attempt.

    Linear does document a 30 minute window in which a failed OAuth refresh may
    be replayed, and that allowance is real. It is also specific to refresh, and
    generalizing it into "retry all Linear POSTs" would quietly extend a
    guarantee about a credential operation to a mutation the provider never made
    that promise about. Any refresh replay therefore belongs to the caller that
    knows it is refreshing, not to this shared path.
    """
    timeout = settings.linear_http_timeout_seconds
    try:
        if client is not None:
            return client.request(method, url, timeout=timeout, **kwargs)
        transport = httpx.HTTPTransport(retries=0)
        with httpx.Client(timeout=timeout, transport=transport) as owned:
            return owned.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise LinearApiError(operation, None, type(exc).__name__) from exc
