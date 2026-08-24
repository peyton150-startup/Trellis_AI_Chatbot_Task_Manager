import os
from urllib.parse import urlparse
from uuid import UUID

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, field_validator, model_validator


load_dotenv()


# D-81. The authorized reasoning ceiling. A configuration may ask for less so the
# budget can be measured downwards later, and may never ask for more.
MODEL_REASONING_BUDGET_CEILING = 6000

# The generation allowance reserved for tool JSON and visible output after
# reasoning has taken its share. Sized against the largest tool call this system
# can legitimately emit, which carries fifty task ids.
MODEL_OUTPUT_HEADROOM_MIN = 4096


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    database_url: str
    model_id: str
    nvidia_api_key: str
    actor_id: UUID
    demo_unsafe_prompt_mode: bool
    app_env: str
    blast_radius_threshold: int
    tool_timeout_seconds: int
    model_timeout_seconds: int
    max_tool_retries: int
    model_reasoning_budget: int
    model_max_tokens: int
    approval_ttl_seconds: int
    lease_ttl_seconds: int

    # T00W. See D-69. The hostname is configured once and both public URLs are
    # derived from it below, because two independently configured hostnames can
    # disagree, and the one that disagrees fails latest: an OAuth redirect
    # mismatch surfaces only at install time, in a browser, against a value
    # Linear stored when the application was registered.
    #
    # Every Linear value defaults to empty. Importing this module must not
    # require a Linear credential, for the same reason `get_agent` is lazy:
    # every deterministic test imports `app.main`. The install CLI and the
    # webhook route validate what they actually need, when they need it.
    trellis_public_origin: str
    linear_client_id: str
    linear_client_secret: str
    linear_webhook_secret: str
    linear_allowed_user_id: str
    linear_oauth_state_ttl_seconds: int
    linear_webhook_freshness_seconds: int
    linear_inbox_lease_seconds: int
    linear_inbox_max_attempts: int
    linear_http_timeout_seconds: int
    linear_worker_poll_seconds: int

    @field_validator("model_reasoning_budget")
    @classmethod
    def _reasoning_budget_within_the_authorized_ceiling(cls, value: int) -> int:
        """D-81. 6000 is an application ceiling, not merely a default.

        NVIDIA's hosted endpoint defaults `reasoning_budget` to 16384, so
        omitting it leaves a provider default in charge of how long Trellis
        spends thinking before it can act. This makes the limit Trellis-owned.

        Lower values are allowed so the budget can be measured downwards later.
        Higher ones are refused outright rather than clamped: silently accepting
        8000 and sending 6000 would make the configuration and the wire disagree,
        which is the kind of difference nobody notices until they are reading a
        latency graph that makes no sense.
        """
        if not 1 <= value <= MODEL_REASONING_BUDGET_CEILING:
            raise ValueError(
                f"MODEL_REASONING_BUDGET must be between 1 and "
                f"{MODEL_REASONING_BUDGET_CEILING}, got {value}"
            )
        return value

    @model_validator(mode="after")
    def _reasoning_leaves_room_to_answer(self) -> "Settings":
        """Reasoning and output share one generation allowance.

        NVIDIA counts reasoning, tool-call JSON, and visible text against the
        same `max_tokens`. A configuration where reasoning could consume the
        whole allowance produces the specific failure of a model that thought
        successfully and then had no room left to say or do anything, which
        surfaces as `finish_reason="length"` with no answer.

        The headroom is deliberately generous: the largest legitimate tool call
        this system can emit carries fifty task ids.
        """
        headroom = self.model_max_tokens - self.model_reasoning_budget
        if headroom < MODEL_OUTPUT_HEADROOM_MIN:
            raise ValueError(
                f"MODEL_MAX_TOKENS must exceed MODEL_REASONING_BUDGET by at "
                f"least {MODEL_OUTPUT_HEADROOM_MIN} so reasoning cannot consume "
                f"the whole generation allowance; got {headroom}"
            )
        return self

    @field_validator("trellis_public_origin")
    @classmethod
    def _canonical_origin(cls, value: str) -> str:
        """One canonical HTTPS origin, or refuse to start.

        Linear requires the `redirect_uri` sent during code exchange to match the
        one used during authorization exactly, and requires the webhook URL to be
        public HTTPS. Both are derived from this value, so a sloppy origin fails
        at install time in a browser against a value Linear stored earlier, which
        is the worst place to discover a trailing slash.

        Canonicalizing here rather than at each use is the point.
        `https://x.ngrok.app` and `https://x.ngrok.app/` would otherwise produce
        two different redirect URIs depending on which caller concatenated them,
        and only one would match what is registered.

        Empty is allowed, because every Linear value defaults to empty so that
        importing this module needs no credential. The install path requires it.
        """
        if not value:
            return value

        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError("TRELLIS_PUBLIC_ORIGIN must use https")
        if not parsed.hostname:
            raise ValueError("TRELLIS_PUBLIC_ORIGIN must carry a host")
        if parsed.username or parsed.password:
            raise ValueError("TRELLIS_PUBLIC_ORIGIN must not carry credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("TRELLIS_PUBLIC_ORIGIN must not carry a query or fragment")
        if parsed.path not in ("", "/"):
            raise ValueError("TRELLIS_PUBLIC_ORIGIN must be an origin, not a path")

        netloc = parsed.hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return f"https://{netloc}"

    @property
    def linear_oauth_redirect_url(self) -> str:
        """The exact value that must be registered in the Linear OAuth app.

        One property, used by both the authorization URL and the token exchange,
        so the two cannot disagree.
        """
        return f"{self.trellis_public_origin}/api/linear/oauth/callback"

    @property
    def linear_webhook_url(self) -> str:
        """The exact value that must be registered as the Linear webhook URL."""
        return f"{self.trellis_public_origin}/api/linear/webhook"


settings = Settings(
    database_url=os.getenv(
        "DATABASE_URL",
        "postgresql://trellis:trellis@localhost:55432/trellis",
    ),
    model_id=os.getenv("MODEL_ID", ""),
    nvidia_api_key=os.getenv("NVIDIA_API_KEY", ""),
    actor_id=os.getenv("ACTOR_ID", "00000000-0000-0000-0000-000000000001"),
    demo_unsafe_prompt_mode=os.getenv("DEMO_UNSAFE_PROMPT_MODE", "false"),
    app_env=os.getenv("APP_ENV", "dev"),
    blast_radius_threshold=os.getenv("BLAST_RADIUS_THRESHOLD", "3"),
    tool_timeout_seconds=os.getenv("TOOL_TIMEOUT_SECONDS", "20"),
    model_timeout_seconds=os.getenv("MODEL_TIMEOUT_SECONDS", "45"),
    max_tool_retries=os.getenv("MAX_TOOL_RETRIES", "2"),
    model_reasoning_budget=os.getenv("MODEL_REASONING_BUDGET", "6000"),
    model_max_tokens=os.getenv("MODEL_MAX_TOKENS", "12288"),
    approval_ttl_seconds=os.getenv("APPROVAL_TTL_SECONDS", "300"),
    lease_ttl_seconds=os.getenv("LEASE_TTL_SECONDS", "120"),
    trellis_public_origin=os.getenv("TRELLIS_PUBLIC_ORIGIN", ""),
    linear_client_id=os.getenv("LINEAR_CLIENT_ID", ""),
    linear_client_secret=os.getenv("LINEAR_CLIENT_SECRET", ""),
    linear_webhook_secret=os.getenv("LINEAR_WEBHOOK_SECRET", ""),
    linear_allowed_user_id=os.getenv("LINEAR_ALLOWED_USER_ID", ""),
    linear_oauth_state_ttl_seconds=os.getenv("LINEAR_OAUTH_STATE_TTL_SECONDS", "600"),
    # Linear's own webhook guidance is to verify the timestamp is within a
    # minute of receipt. This is that minute, and it is configurable only so a
    # deterministic test can pin it, not so an operator can widen the replay
    # window in production.
    linear_webhook_freshness_seconds=os.getenv("LINEAR_WEBHOOK_FRESHNESS_SECONDS", "60"),
    linear_inbox_lease_seconds=os.getenv("LINEAR_INBOX_LEASE_SECONDS", "120"),
    linear_inbox_max_attempts=os.getenv("LINEAR_INBOX_MAX_ATTEMPTS", "5"),
    linear_http_timeout_seconds=os.getenv("LINEAR_HTTP_TIMEOUT_SECONDS", "15"),
    # How long the worker sleeps when the inbox is empty. Polling rather than
    # LISTEN/NOTIFY because the inbox already has a durable claim protocol and
    # a notification adds a second delivery path that can be missed across a
    # restart. Two seconds is well inside what a conversational reply tolerates.
    linear_worker_poll_seconds=os.getenv("LINEAR_WORKER_POLL_SECONDS", "2"),
)

if settings.demo_unsafe_prompt_mode and settings.app_env != "demo":
    raise RuntimeError(
        "DEMO_UNSAFE_PROMPT_MODE requires APP_ENV=demo. "
        "This flag disables the untrusted-data boundary and exists "
        "only to demonstrate the boundary failing."
    )
