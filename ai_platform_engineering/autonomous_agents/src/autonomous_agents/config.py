"""Configuration settings for Autonomous Agents service."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8002
    debug: bool = False

    # LLM (passed through to agents via A2A)
    llm_provider: str = "anthropic-claude"

    # Supervisor A2A endpoint — autonomous agents send tasks here
    supervisor_url: str = "http://localhost:8000"

    # A2A call timeout (seconds) for the per-attempt HTTP request to the
    # supervisor. The previous implementation hard-coded this to 300; it is
    # now overridable per environment and per task (see TaskDefinition).
    a2a_timeout_seconds: float = Field(default=300.0, gt=0)

    # Maximum *additional* retry attempts after the initial request when the
    # supervisor returns a 5xx status or the transport fails. 0 disables
    # retries (single attempt). 4xx responses are never retried — those
    # signal a client-side error that retrying cannot fix.
    a2a_max_retries: int = Field(default=3, ge=0)

    # Initial backoff (seconds) for the first retry. Exposed mainly so
    # tests can drive the retry loop without sleeping for real seconds;
    # production tuning should usually leave this at 1.
    a2a_retry_backoff_initial_seconds: float = Field(default=1.0, ge=0)

    # Maximum backoff (seconds) between retry attempts. Backoff is
    # exponential with jitter starting at ``a2a_retry_backoff_initial_seconds``;
    # this caps the upper bound so a long-degraded supervisor cannot
    # stall a run for arbitrarily long.
    a2a_retry_backoff_max_seconds: float = Field(default=30.0, gt=0)

    @field_validator(
        "a2a_timeout_seconds",
        "a2a_retry_backoff_initial_seconds",
        "a2a_retry_backoff_max_seconds",
    )
    @classmethod
    def _reject_nonfinite(cls, v: float) -> float:
        # Pydantic happily accepts inf/nan from env vars cast to float;
        # both would silently break httpx (timeout) or tenacity (wait).
        # Sign / non-negative bounds are enforced separately by the
        # per-field ``gt=0`` / ``ge=0`` constraints — this validator is
        # *only* responsible for the finiteness check.
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        return v

    # Path to the YAML file that defines scheduled tasks
    task_config_path: str = "config.yaml"

    # Webhook secret for validating incoming webhook payloads (optional)
    webhook_secret: str | None = None

    # CORS — keep empty by default; open only in explicit dev/test configs
    cors_origins: list[str] = []

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):
        # Accept either a JSON list (native pydantic-settings behaviour)
        # OR a plain comma-separated string from `.env`. Operators
        # routinely paste the latter ("http://localhost:3000,https://app.example.com")
        # and would otherwise hit an opaque parse error. Strings are
        # split on commas and whitespace-trimmed.
        if isinstance(v, str):
            v = [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("cors_origins")
    @classmethod
    def _reject_unsafe_cors_wildcard(cls, v: list[str]) -> list[str]:
        # IMP-05: ``cors_origins=["*"]`` plus ``allow_credentials=True``
        # (the FastAPI default in main.py) is a CORS spec violation --
        # browsers refuse the response and you get cryptic "credentialed
        # request rejected" errors at runtime. Worse, some misconfigured
        # gateways DO honour it and expose every authenticated route to
        # any origin. Fail fast at startup so the misconfig is obvious.
        if any(origin.strip() == "*" for origin in v):
            raise ValueError(
                "cors_origins=['*'] is unsafe with allow_credentials=True; "
                "list each allowed origin explicitly (e.g. "
                "['http://localhost:3000','https://app.example.com'])"
            )
        return v

    # MongoDB persistence for run history (optional).
    # Both must be set to enable MongoRunStore; otherwise the service
    # falls back to a bounded in-memory store (legacy behaviour) so
    # development environments need no external infrastructure.
    mongodb_uri: str | None = None
    mongodb_database: str | None = None
    mongodb_collection: str = "autonomous_runs"

    # MongoDB collection that holds task definitions (the source of
    # truth for CRUD operations). Only used when both ``mongodb_uri``
    # and ``mongodb_database`` are set; otherwise the in-memory
    # TaskStore is used and tasks are seeded from YAML on every boot.
    mongodb_tasks_collection: str = "autonomous_tasks"

    # Maximum runs retained by the in-memory store when Mongo is not
    # configured. Ignored when MongoRunStore is in use.
    run_history_maxlen: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
