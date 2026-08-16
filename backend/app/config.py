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
)

if settings.demo_unsafe_prompt_mode and settings.app_env != "demo":
    raise RuntimeError(
        "DEMO_UNSAFE_PROMPT_MODE requires APP_ENV=demo. "
        "This flag disables the untrusted-data boundary and exists "
        "only to demonstrate the boundary failing."
    )
