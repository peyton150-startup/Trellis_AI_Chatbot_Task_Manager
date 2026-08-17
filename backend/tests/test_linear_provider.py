"""T00W provider boundary tests. Offline, no credential, no live workspace.

Every test drives `linear_agent_api` through an `httpx.MockTransport`, which is
the seam the module's `client` parameter exists for. Nothing here reaches the
network, so this file carries no `network` marker and runs in the default CI
collection.

Four properties are under test, and they are the four D-69 requires of the
boundary before the harder state machine is built on top of it:

```text
correct requests            the right endpoint, method, and parameters
validated responses         typed results, never raw response.json()
secret-safe failures        no token or secret in any exception
no unjustified retries      one call, one outbound mutation attempt
```
"""

import json

import httpx
import pytest

from app import linear_agent_api as api
from app.linear_agent_api import LinearApiError


def _client(handler) -> httpx.Client:
    """An httpx client whose transport is a callable, with retries impossible."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def _counting(response_factory):
    """A handler that records every request it is asked to make.

    Returns the handler and the list it appends to, so a test can assert on the
    number of outbound attempts rather than only on the returned value. That
    count is the whole point of the no-retry requirement: a retry is invisible
    to a caller that only inspects the final exception.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response_factory(request)

    return handler, seen


def _with_settings(monkeypatch, **overrides) -> None:
    """Swap the module's `settings` for a copy carrying the given overrides.

    `Settings` is frozen, so `setattr` on it raises. Replacing the module
    attribute is also the more honest substitution: it proves the code reads
    `settings` at call time rather than having captured a value at import.
    """
    monkeypatch.setattr(api, "settings", api.settings.model_copy(update=overrides))


def _token_body(**overrides) -> dict:
    body = {
        "access_token": "at-live",
        "refresh_token": "rt-live",
        "expires_in": 86399,
        "token_type": "Bearer",
        "scope": "read,write,app:mentionable,app:assignable",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------- requests


def test_authorization_url_carries_actor_app_and_derived_redirect(monkeypatch):
    """`actor=app` is what makes this an agent install rather than a user one."""
    _with_settings(
        monkeypatch,
        linear_client_id="cid",
        trellis_public_origin="https://demo.example",
    )

    url = api.authorization_url("state-123")

    assert url.startswith(api.LINEAR_AUTHORIZE_URL + "?")
    assert "actor=app" in url
    assert "state=state-123" in url
    # Derived from the single configured origin, not separately configured.
    assert "redirect_uri=https%3A%2F%2Fdemo.example%2Fapi%2Flinear%2Foauth%2Fcallback" in url


def test_new_oauth_state_is_unguessable_and_unique():
    values = {api.new_oauth_state() for _ in range(50)}
    assert len(values) == 50
    assert all(len(value) >= 32 for value in values)


def test_exchange_posts_the_documented_grant_to_the_token_endpoint():
    handler, seen = _counting(lambda _r: httpx.Response(200, json=_token_body()))
    with _client(handler) as client:
        tokens = api.exchange_code("the-code", client=client)

    assert len(seen) == 1
    assert str(seen[0].url) == api.LINEAR_TOKEN_URL
    assert b"grant_type=authorization_code" in seen[0].content
    assert tokens.access_token == "at-live"
    assert tokens.expires_in == 86399


def test_refresh_returns_the_rotated_pair_expiry_and_scope():
    """Linear rotates the refresh token, so the response replaces the input."""
    handler, seen = _counting(
        lambda _r: httpx.Response(
            200, json=_token_body(access_token="at-2", refresh_token="rt-2")
        )
    )
    with _client(handler) as client:
        tokens = api.refresh_tokens("rt-1", client=client)

    assert len(seen) == 1
    assert b"grant_type=refresh_token" in seen[0].content
    assert tokens.access_token == "at-2"
    assert tokens.refresh_token == "rt-2"
    assert tokens.scope == "read,write,app:mentionable,app:assignable"


def test_viewer_query_is_sent_with_bearer_auth():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer at-live"
        return httpx.Response(200, json={"data": {"viewer": {"id": "app-user-1"}}})

    handler, seen = _counting(respond)
    with _client(handler) as client:
        assert api.fetch_app_user_id("at-live", client=client) == "app-user-1"
    assert str(seen[0].url) == api.LINEAR_GRAPHQL_URL


def test_agent_activity_sends_the_caller_supplied_uuid():
    """The caller owns the id so a retry could reuse it. See D-69 and the probe."""
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["variables"]["input"]["id"] == "11111111-1111-4111-8111-111111111111"
        assert body["variables"]["input"]["agentSessionId"] == "sess-1"
        return httpx.Response(
            200,
            json={"data": {"agentActivityCreate": {"success": True, "agentActivity": {"id": "act-1"}}}},
        )

    handler, seen = _counting(respond)
    with _client(handler) as client:
        api.create_agent_activity(
            "at-live",
            agent_session_id="sess-1",
            activity_id="11111111-1111-4111-8111-111111111111",
            content={"type": "thought", "body": "Checking your Trellis tasks."},
            client=client,
        )
    assert len(seen) == 1


# ------------------------------------------------------- validated responses


def test_array_shaped_scope_fails_closed():
    """Linear's legacy array shape cannot reach a text column.

    Applications created before 1 December 2023 can receive `scope` as an array.
    The Trellis application is new, so this is chosen to fail closed rather than
    be normalized: silently coercing a list into a `text` column moves the
    failure somewhere later and harder to attribute.
    """
    handler, _ = _counting(
        lambda _r: httpx.Response(200, json=_token_body(scope=["read", "write"]))
    )
    with _client(handler) as client, pytest.raises(LinearApiError) as caught:
        api.exchange_code("code", client=client)

    assert "scope" in caught.value.detail


@pytest.mark.parametrize(
    "overrides",
    [
        {"access_token": ""},
        {"access_token": 12345},
        {"expires_in": "soon"},
        {"expires_in": 0},
        {"token_type": None},
    ],
    ids=["empty-token", "numeric-token", "string-expiry", "zero-expiry", "null-type"],
)
def test_malformed_token_responses_are_refused(overrides):
    handler, _ = _counting(
        lambda _r: httpx.Response(200, json=_token_body(**overrides))
    )
    with _client(handler) as client, pytest.raises(LinearApiError):
        api.exchange_code("code", client=client)


def test_viewer_without_an_id_is_refused():
    handler, _ = _counting(lambda _r: httpx.Response(200, json={"data": {"viewer": {}}}))
    with _client(handler) as client, pytest.raises(LinearApiError):
        api.fetch_app_user_id("at-live", client=client)


def test_graphql_partial_success_is_a_failure():
    """HTTP 200 with usable-looking `data` AND `errors` must not read as success.

    Linear states a query can partially succeed and return both, and that clients
    must check `errors` before assuming success. A caller inspecting only the
    status code, or only the presence of `data`, would accept this.
    """
    handler, _ = _counting(
        lambda _r: httpx.Response(
            200,
            json={
                "data": {"viewer": {"id": "usable-looking"}},
                "errors": [{"message": "partial failure"}],
            },
        )
    )
    with _client(handler) as client, pytest.raises(LinearApiError) as caught:
        api.fetch_app_user_id("at-live", client=client)

    assert caught.value.detail == "partial failure"


def test_activity_success_false_is_refused():
    handler, _ = _counting(
        lambda _r: httpx.Response(
            200,
            json={"data": {"agentActivityCreate": {"success": False, "agentActivity": {"id": "a"}}}},
        )
    )
    with _client(handler) as client, pytest.raises(LinearApiError):
        api.create_agent_activity(
            "at-live",
            agent_session_id="s",
            activity_id="i",
            content={},
            client=client,
        )


def test_activity_success_without_an_activity_is_refused():
    """A payload claiming success while carrying no activity is not success."""
    handler, _ = _counting(
        lambda _r: httpx.Response(
            200, json={"data": {"agentActivityCreate": {"success": True}}}
        )
    )
    with _client(handler) as client, pytest.raises(LinearApiError):
        api.create_agent_activity(
            "at-live",
            agent_session_id="s",
            activity_id="i",
            content={},
            client=client,
        )


# ------------------------------------------------------ secret-safe failures


def test_no_secret_appears_in_a_token_failure(monkeypatch):
    """The token form carries a client secret; a failure must not echo it."""
    _with_settings(monkeypatch, linear_client_secret="SUPERSECRET")
    handler, _ = _counting(
        lambda _r: httpx.Response(401, json={"error": "invalid_client", "sent": "SUPERSECRET"})
    )
    with _client(handler) as client, pytest.raises(LinearApiError) as caught:
        api.exchange_code("code", client=client)

    assert "SUPERSECRET" not in str(caught.value)


def test_no_bearer_token_appears_in_a_graphql_failure():
    handler, _ = _counting(
        lambda _r: httpx.Response(
            200, json={"errors": [{"message": "bad auth", "extensions": {"token": "at-live"}}]}
        )
    )
    with _client(handler) as client, pytest.raises(LinearApiError) as caught:
        api.fetch_app_user_id("at-live", client=client)

    assert "at-live" not in str(caught.value)


# ----------------------------------------------------- no unjustified retries


def test_agent_activity_transport_failure_issues_exactly_one_attempt():
    """The ambiguous remote-side-effect case, and the reason retries are absent.

    A connection that dies after transmission leaves two indistinguishable
    states: Linear never received the mutation, or Linear committed it and the
    response was lost. Retrying picks one by guessing, and the guess is safe only
    if replaying the same activity UUID is proven idempotent at the provider.
    That probe needs a live workspace and has not run.

    Asserting on the raised error is not enough, because a retry is invisible in
    the exception. The attempt count is the assertion.
    """
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("connection reset after send")

    with _client(handler) as client, pytest.raises(LinearApiError) as caught:
        api.create_agent_activity(
            "at-live",
            agent_session_id="s",
            activity_id="i",
            content={},
            client=client,
        )

    assert len(attempts) == 1
    # status=None distinguishes unreachable from refused, which is what lets a
    # caller decide. It never means "safe to repeat".
    assert caught.value.status is None


def test_agent_activity_server_error_issues_exactly_one_attempt():
    handler, seen = _counting(lambda _r: httpx.Response(500, text="upstream boom"))

    with _client(handler) as client, pytest.raises(LinearApiError):
        api.create_agent_activity(
            "at-live",
            agent_session_id="s",
            activity_id="i",
            content={},
            client=client,
        )

    assert len(seen) == 1


def test_the_module_declares_no_retry_policy():
    """A grep-style guard against a retry arriving later by accident.

    The no-retry property is a decision with a reason, not an accident of httpx's
    defaults, and the constructed transport pins it. If someone later adds a
    retry count here, this fails and sends them to read why it is zero.
    """
    source = (api.__file__ or "")
    assert source
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "retries=0" in text
    assert "retries=1" not in text
