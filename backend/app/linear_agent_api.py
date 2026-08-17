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
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

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

_GRAPHQL_VIEWER = "query { viewer { id } }"
_GRAPHQL_CREATE_ACTIVITY = """
mutation CreateAgentActivity($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) {
    success
    agentActivity { id }
  }
}
"""


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


@dataclass(frozen=True, slots=True)
class LinearTokens:
    """One token response. The shape is identical for exchange and refresh.

    `refresh_token` is optional because Linear's client-credentials style
    responses omit it. The caller persists whatever came back and must not
    assume the previous refresh token survives, since Linear rotates it.
    """

    access_token: str
    refresh_token: str | None
    expires_in: int
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


def revoke_token(access_token: str, *, client: httpx.Client | None = None) -> None:
    """Revoke a token at the provider.

    Returns cleanly on success. A provider-side failure raises, and the caller
    decides whether local revocation should proceed anyway. It usually should:
    an installation we can no longer use is revoked locally whether or not the
    remote call succeeded, and leaving it `active` because a network call failed
    would be the less safe direction.
    """
    response = _request(
        "oauth_revoke",
        "POST",
        LINEAR_REVOKE_URL,
        client=client,
        data={"token": access_token},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if response.status_code != 200:
        raise LinearApiError("oauth_revoke", response.status_code, "revocation refused")


def fetch_app_user_id(access_token: str, *, client: httpx.Client | None = None) -> str:
    """The installed application's workspace-specific identity, `viewer.id`.

    Linear recommends storing this per workspace so the app can identify itself,
    and T00W needs it as an authorization input: an `AgentSessionEvent` carries
    `appUserId`, and a webhook whose value does not match the stored one is not
    for this installation. Read only, and the only non-activity GraphQL query
    D-69 authorizes.
    """
    data = _post_graphql("viewer", access_token, _GRAPHQL_VIEWER, {}, client=client)
    viewer = data.get("viewer") or {}
    app_user_id = viewer.get("id")
    if not isinstance(app_user_id, str) or not app_user_id:
        raise LinearApiError("viewer", None, "response carried no viewer id")
    return app_user_id


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
    result = data.get("agentActivityCreate") or {}
    if not result.get("success"):
        raise LinearApiError(
            "agent_activity_create", None, "provider reported success=false"
        )


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

    access_token = body.get("access_token")
    expires_in = body.get("expires_in")
    if not isinstance(access_token, str) or not isinstance(expires_in, int):
        raise LinearApiError(operation, response.status_code, "response was malformed")

    return LinearTokens(
        access_token=access_token,
        refresh_token=body.get("refresh_token"),
        expires_in=expires_in,
        scope=body.get("scope") or "",
    )


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
    "Linear said no" from "Linear was unreachable". The worker retries the second
    and generally should not retry the first.
    """
    timeout = settings.linear_http_timeout_seconds
    try:
        if client is not None:
            return client.request(method, url, timeout=timeout, **kwargs)
        with httpx.Client(timeout=timeout) as owned:
            return owned.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise LinearApiError(operation, None, type(exc).__name__) from exc
