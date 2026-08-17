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

    Constructed through the class rather than `model_copy(update=...)`, and that
    difference is not cosmetic. `model_copy` skips validators, so a helper built
    on it hands the code under test an origin the real startup path would have
    canonicalized or rejected. That is a harness quietly disabling the thing
    under test, and it produced a double slash in the redirect URI before this
    was corrected.
    """
    values = {**api.settings.model_dump(), **overrides}
    monkeypatch.setattr(api, "settings", api.settings.__class__(**values))


def _token_body(**overrides) -> dict:
    body = {
        "access_token": "at-live",
        "refresh_token": "rt-live",
        "expires_in": 86399,
        "token_type": "Bearer",
        "scope": "read write app:mentionable app:assignable",
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
    assert api.granted_scope_set(tokens) == set(api.LINEAR_SCOPES)
    assert api.has_bearer_token_type(tokens)


def test_identity_query_is_sent_with_bearer_auth():
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer at-live"
        return httpx.Response(200, json={"data": {"viewer": {"id": "app-user-1", "organization": {"id": "org-1"}}}})

    handler, seen = _counting(respond)
    with _client(handler) as client:
        identity = api.fetch_installation_identity("at-live", client=client)
        assert identity.app_user_id == "app-user-1"
        assert identity.organization_id == "org-1"
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


def test_identity_without_an_organization_is_refused():
    handler, _ = _counting(
        lambda _r: httpx.Response(200, json={"data": {"viewer": {"id": "app-user-1"}}})
    )
    with _client(handler) as client, pytest.raises(LinearApiError):
        api.fetch_installation_identity("at-live", client=client)


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
                "data": {"viewer": {"id": "usable-looking", "organization": {"id": "o"}}},
                "errors": [{"message": "partial failure"}],
            },
        )
    )
    with _client(handler) as client, pytest.raises(LinearApiError) as caught:
        api.fetch_installation_identity("at-live", client=client)

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
        api.fetch_installation_identity("at-live", client=client)

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


# ------------------------------------------------- D-70 contract corrections


def test_revoke_sends_the_modern_form_and_no_bearer_header():
    """`token` plus an optional hint, and nothing else. See D-70.

    An earlier version also sent the token as a bearer header. The danger of that
    is not a loud failure but a quiet one: the endpoint accepts the call and the
    caller believes a credential was revoked when it was not.
    """
    handler, seen = _counting(lambda _r: httpx.Response(200, json={}))
    with _client(handler) as client:
        api.revoke_token("rt-1", token_type_hint="refresh_token", client=client)

    request = seen[0]
    assert str(request.url) == api.LINEAR_REVOKE_URL
    assert b"token=rt-1" in request.content
    assert b"token_type_hint=refresh_token" in request.content
    assert "Authorization" not in request.headers
    # The legacy field names must not appear.
    assert b"access_token=" not in request.content
    assert b"refresh_token=" not in request.content


def test_revoke_without_a_hint_omits_the_field():
    handler, seen = _counting(lambda _r: httpx.Response(200, json={}))
    with _client(handler) as client:
        api.revoke_token("at-1", client=client)

    assert b"token_type_hint" not in seen[0].content


def test_revoke_failure_raises_so_the_caller_can_decide():
    handler, seen = _counting(lambda _r: httpx.Response(400, json={}))
    with _client(handler) as client, pytest.raises(LinearApiError):
        api.revoke_token("at-1", client=client)
    assert len(seen) == 1


def test_authorization_url_scope_is_comma_separated(monkeypatch):
    """The request format. The response format is different, deliberately.

    Scope goes out comma-separated and comes back space-delimited. Code that
    assumes one round-trips is wrong in a way only the live provider reveals,
    which is why both directions are pinned by tests rather than by memory.
    """
    _with_settings(
        monkeypatch, linear_client_id="cid", trellis_public_origin="https://x.example"
    )
    url = api.authorization_url("s")
    assert "scope=read%2Cwrite%2Capp%3Amentionable%2Capp%3Aassignable" in url


def test_granted_scope_set_ignores_order_and_format():
    """Set comparison, never the raw string and never ordering."""
    handler, _ = _counting(
        lambda _r: httpx.Response(
            200,
            json=_token_body(scope="app:assignable read app:mentionable write"),
        )
    )
    with _client(handler) as client:
        tokens = api.exchange_code("code", client=client)

    assert api.granted_scope_set(tokens) == set(api.LINEAR_SCOPES)


def test_missing_scope_is_detected_as_a_set_difference():
    handler, _ = _counting(
        lambda _r: httpx.Response(200, json=_token_body(scope="read write"))
    )
    with _client(handler) as client:
        tokens = api.exchange_code("code", client=client)

    granted = api.granted_scope_set(tokens)
    assert granted != set(api.LINEAR_SCOPES)
    assert set(api.LINEAR_SCOPES) - granted == {"app:mentionable", "app:assignable"}


@pytest.mark.parametrize("token_type", ["Bearer", "bearer", "BEARER"])
def test_bearer_token_type_is_compared_case_insensitively(token_type):
    """The provider documents `Bearer`; the capitalization is not the contract."""
    handler, _ = _counting(
        lambda _r: httpx.Response(200, json=_token_body(token_type=token_type))
    )
    with _client(handler) as client:
        assert api.has_bearer_token_type(api.exchange_code("code", client=client))


def test_non_bearer_token_type_is_rejected_by_the_caller_check():
    handler, _ = _counting(
        lambda _r: httpx.Response(200, json=_token_body(token_type="mac"))
    )
    with _client(handler) as client:
        assert not api.has_bearer_token_type(api.exchange_code("code", client=client))


def test_identity_query_asks_for_the_organization():
    """D-70's whole point: organization_id cannot be sourced anywhere else."""
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["query"] = json.loads(request.content)["query"]
        return httpx.Response(
            200,
            json={"data": {"viewer": {"id": "u", "organization": {"id": "o"}}}},
        )

    handler, _ = _counting(respond)
    with _client(handler) as client:
        api.fetch_installation_identity("at-live", client=client)

    assert "organization" in captured["query"]
    # Still installation identity, not workspace resolution.
    for forbidden in ("organizations(", "teams(", "issues(", "search"):
        assert forbidden not in captured["query"]


def test_identity_with_an_empty_organization_id_is_refused():
    handler, _ = _counting(
        lambda _r: httpx.Response(
            200, json={"data": {"viewer": {"id": "u", "organization": {"id": ""}}}}
        )
    )
    with _client(handler) as client, pytest.raises(LinearApiError):
        api.fetch_installation_identity("at-live", client=client)


# ------------------------------------------------- public origin validation


@pytest.mark.parametrize(
    "origin",
    [
        "http://foo.ngrok.app",
        "ftp://foo.ngrok.app",
        "https://foo.ngrok.app/x",
        "https://foo.ngrok.app/api/linear",
        "https://foo.ngrok.app?x=y",
        "https://foo.ngrok.app#frag",
        "https://user:pw@foo.ngrok.app",
        "https://",
        "notaurl",
    ],
    ids=[
        "http", "ftp", "path", "deep-path", "query", "fragment",
        "credentials", "no-host", "garbage",
    ],
)
def test_malformed_public_origins_are_rejected(origin):
    """A bad origin must fail at startup, not at install time in a browser.

    Linear requires the redirect_uri at code exchange to match the one used at
    authorization exactly. A wrong origin therefore surfaces against a value
    Linear stored earlier, which is the worst possible place to find a trailing
    slash or a stray path.
    """
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        api.settings.model_copy().__class__(
            **{**api.settings.model_dump(), "trellis_public_origin": origin}
        )


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://foo.ngrok.app", "https://foo.ngrok.app"),
        ("https://foo.ngrok.app/", "https://foo.ngrok.app"),
        ("https://FOO.ngrok.app", "https://foo.ngrok.app"),
        ("https://foo.ngrok.app:8443", "https://foo.ngrok.app:8443"),
    ],
    ids=["bare", "trailing-slash", "uppercase-host", "explicit-port"],
)
def test_public_origins_canonicalize_identically(origin, expected):
    """Both spellings must produce one redirect URI, or only one would match."""
    settings = api.settings.__class__(
        **{**api.settings.model_dump(), "trellis_public_origin": origin}
    )
    assert settings.trellis_public_origin == expected
    assert settings.linear_oauth_redirect_url == f"{expected}/api/linear/oauth/callback"
    assert settings.linear_webhook_url == f"{expected}/api/linear/webhook"


def test_empty_origin_is_allowed_so_import_needs_no_credential():
    settings = api.settings.__class__(
        **{**api.settings.model_dump(), "trellis_public_origin": ""}
    )
    assert settings.trellis_public_origin == ""


def test_authorization_url_and_exchange_use_the_same_redirect(monkeypatch):
    """One property feeds both, so they cannot drift apart."""
    _with_settings(
        monkeypatch, linear_client_id="cid", trellis_public_origin="https://x.ngrok.app/"
    )
    handler, seen = _counting(lambda _r: httpx.Response(200, json=_token_body()))
    with _client(handler) as client:
        api.exchange_code("code", client=client)

    expected = "https://x.ngrok.app/api/linear/oauth/callback"
    assert expected in api.authorization_url("s").replace("%3A", ":").replace("%2F", "/")
    assert expected.encode() in seen[0].content.replace(b"%3A", b":").replace(b"%2F", b"/")
