"""T00W OAuth installation: the operator CLI and the callback. D-69, D-70.

Installation is operator-initiated rather than exposed as a public route. There
is no `GET /api/linear/install`, and that absence is the design: an unauthenticated
endpoint that mints OAuth state and hands back an authorization URL lets anyone
on the internet start an installation flow against this deployment. The operator
runs a command instead, and the only public surface is the callback, which is
useless without a state value this process generated.

```powershell
python -m app.linear_install
```

**The transaction boundaries below are a correctness property, not a style
choice, and D-70 freezes them:**

```text
TX A   consume the OAuth state, guarded      COMMIT
       (no transaction open)
       exchange the code
       validate the token contract
       fetch installation identity
TX B   insert the ACTIVE installation        COMMIT
```

No database transaction is held open across a call to Linear. Holding one would
pin a connection for the duration of a provider round trip, and a provider that
hangs would hold it for the full timeout. More importantly, committing the state
consumption *before* the exchange is what makes a crash safe: the state is spent
either way, so a second attempt with the same authorization code cannot replay a
state value that still looks unconsumed.

That ordering leaves one window this schema cannot close. Linear can issue
credentials and this process can die before they are persisted, orphaning a token
pair. Cleanup on a caught failure is best effort and **does not eliminate that
case**: it handles failures we catch, not process death. D-70 records the window
and declines to add token staging state for a demo-scale risk.
"""

from __future__ import annotations

import hashlib
import sys

from psycopg import errors

from . import linear_agent_api as provider
from . import sql
from .config import settings
from .db import pool
from .linear_agent_api import LinearApiError


class InstallationError(RuntimeError):
    """Installation failed. The message is safe to show and never carries a secret."""


def state_hash(state: str) -> str:
    """What is stored. The raw state never touches the database.

    A stored raw state would let anyone with read access replay an in-flight
    authorization. The hash is sufficient because the callback presents the
    original value and the comparison happens on the digest.
    """
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def begin_installation() -> str:
    """Create one single-use state and return the URL for the operator to open."""
    _require(settings.linear_client_id, "LINEAR_CLIENT_ID")
    _require(settings.linear_client_secret, "LINEAR_CLIENT_SECRET")
    _require(settings.trellis_public_origin, "TRELLIS_PUBLIC_ORIGIN")
    _require(settings.linear_allowed_user_id, "LINEAR_ALLOWED_USER_ID")

    state = provider.new_oauth_state()
    with pool.connection() as conn:
        conn.execute(
            sql.INSERT_LINEAR_OAUTH_STATE,
            {
                "state_hash": state_hash(state),
                "ttl_seconds": settings.linear_oauth_state_ttl_seconds,
            },
        )
        conn.commit()
    return provider.authorization_url(state)


def consume_state(state: str) -> bool:
    """TX A. Spend the state exactly once, or refuse. Committed before any call.

    Matching, unexpired, and unconsumed are predicates on the UPDATE rather than
    checks performed first, so two concurrent callbacks presenting one state
    cannot both proceed. The caller learns only whether it succeeded: which of
    the three reasons applied is deliberately not reported, because a callback is
    a public endpoint and the distinction would tell a prober whether a state
    value ever existed.
    """
    with pool.connection() as conn:
        row = conn.execute(
            sql.CONSUME_LINEAR_OAUTH_STATE, {"state_hash": state_hash(state)}
        ).fetchone()
        conn.commit()
    return row is not None


def complete_installation(code: str, state: str) -> dict:
    """The callback's whole job. See the module docstring for the phase split."""
    if not settings.linear_webhook_secret or not settings.linear_allowed_user_id:
        raise InstallationError("installation is not configured on this server")

    # TX A, committed before anything reaches the network.
    if not consume_state(state):
        raise InstallationError("this installation link is no longer valid")

    # No transaction open from here until TX B.
    try:
        tokens = provider.exchange_code(code)
    except LinearApiError:
        raise InstallationError("the authorization code could not be exchanged") from None

    try:
        _assert_token_contract(tokens)
        identity = provider.fetch_installation_identity(tokens.access_token)
    except (LinearApiError, InstallationError) as exc:
        _best_effort_revoke(tokens)
        raise InstallationError(str(exc)) from None

    # TX B.
    try:
        with pool.connection() as conn:
            row = conn.execute(
                sql.INSERT_LINEAR_INSTALLATION,
                {
                    "organization_id": identity.organization_id,
                    "oauth_client_id": settings.linear_client_id,
                    "app_user_id": identity.app_user_id,
                    "allowed_linear_user_id": settings.linear_allowed_user_id,
                    "access_token": tokens.access_token,
                    "refresh_token": tokens.refresh_token,
                    "expires_in": tokens.expires_in,
                    "granted_scopes": tokens.scope,
                },
            ).fetchone()
            conn.commit()
    except errors.UniqueViolation:
        # The partial unique index is the authority on one ACTIVE installation,
        # and it is doing its job here rather than a pre-check that could race.
        _best_effort_revoke(tokens)
        raise InstallationError(
            "an active Linear installation already exists; revoke it first"
        ) from None
    except Exception:
        _best_effort_revoke(tokens)
        raise

    return {"organization_id": row["organization_id"], "app_user_id": row["app_user_id"]}


def _assert_token_contract(tokens) -> None:
    """What an authorization-code exchange must return. D-70.

    Stricter than the generic provider model on purpose. That model also
    describes responses other flows produce, while this flow specifically needs a
    refresh token, a live expiry, bearer semantics, and the exact scope set.

    The scope comparison is a set comparison. Scope is comma-separated in the
    authorization URL and space-delimited in the response, so comparing strings
    would compare two different formats, and comparing order would depend on
    something the provider never promised.
    """
    if not tokens.refresh_token:
        raise InstallationError("the token response carried no refresh token")
    if not provider.has_bearer_token_type(tokens):
        raise InstallationError("the token response was not a bearer token")

    granted = provider.granted_scope_set(tokens)
    required = set(provider.LINEAR_SCOPES)
    if granted != required:
        missing = sorted(required - granted)
        # Scope names are not secret and naming them is the difference between a
        # fixable message and a mystery. An install that failed because the app
        # lacks `app:mentionable` should say so.
        raise InstallationError(
            f"the installation did not grant the required scopes: {', '.join(missing)}"
            if missing
            else "the installation granted unexpected scopes"
        )


def _best_effort_revoke(tokens) -> None:
    """Try to hand back credentials from an installation that failed.

    Each token is attempted independently so the second still runs if the first
    fails, and every failure is swallowed. A cleanup error must not replace the
    original installation failure in the operator's message: the reason the
    install failed is the useful information, and reporting a revoke failure
    instead would bury it.

    **This does not guarantee no orphaned credential exists.** It runs only on
    failures this process catches. A crash between the exchange and persistence
    leaves a live token pair that nothing here can reach. See D-70.
    """
    for token, hint in (
        (tokens.refresh_token, "refresh_token"),
        (tokens.access_token, "access_token"),
    ):
        if not token:
            continue
        try:
            provider.revoke_token(token, token_type_hint=hint)
        except Exception:  # noqa: BLE001 - cleanup must never mask the real failure
            pass


def _require(value: str, name: str) -> None:
    if not value:
        raise InstallationError(f"{name} is required to start an installation")


def main() -> int:
    """Print the authorization URL for the operator to open in a browser."""
    try:
        url = begin_installation()
    except InstallationError as exc:
        print(f"cannot start installation: {exc}", file=sys.stderr)
        return 1

    print("Open this URL in a browser signed in as the Linear workspace admin:")
    print()
    print(url)
    print()
    print(f"Linear will redirect to {settings.linear_oauth_redirect_url}")
    print(f"This link is single use and expires in {settings.linear_oauth_state_ttl_seconds} seconds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
