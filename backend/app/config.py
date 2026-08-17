import os
from uuid import UUID

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict


load_dotenv()


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

    @property
    def linear_oauth_redirect_url(self) -> str:
        """The exact value that must be registered in the Linear OAuth app."""
        return f"{self.trellis_public_origin.rstrip('/')}/api/linear/oauth/callback"

    @property
    def linear_webhook_url(self) -> str:
        """The exact value that must be registered as the Linear webhook URL."""
        return f"{self.trellis_public_origin.rstrip('/')}/api/linear/webhook"


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
)

if settings.demo_unsafe_prompt_mode and settings.app_env != "demo":
    raise RuntimeError(
        "DEMO_UNSAFE_PROMPT_MODE requires APP_ENV=demo. "
        "This flag disables the untrusted-data boundary and exists "
        "only to demonstrate the boundary failing."
    )
