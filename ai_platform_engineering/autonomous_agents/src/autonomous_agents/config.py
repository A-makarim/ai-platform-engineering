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

    # ------------------------------------------------------------------
    # Dynamic-agents service routing.
    # ------------------------------------------------------------------
    # When a TaskDefinition has ``dynamic_agent_id`` set, the scheduler
    # and preflight bypass the supervisor and call the dynamic-agents
    # service instead so the prompt actually executes through the
    # user's custom agent (its tools / system prompt / middleware),
    # rather than being silently swallowed by the supervisor's
    # permissive LLM router.
    #
    # ``dynamic_agents_url`` is intentionally optional: deployments that
    # never use custom agents leave it unset and dynamic-agent tasks
    # surface as a clear ack failure ("DYNAMIC_AGENTS_URL is not
    # configured ...") instead of a silent transport error.
    dynamic_agents_url: str | None = None

    # Synthetic email stamped into the X-User-Context header that
    # autonomous-agents sends to the dynamic-agents service. The
    # dynamic-agents auth layer trusts whatever the gateway puts in
    # this header (see dynamic_agents/auth/auth.py); here the
    # "gateway" is the autonomous-agents service itself running a
    # system-driven task with no human user attached.
    dynamic_agents_system_email: str = "autonomous@system"

    # Per-call HTTP timeout for ``POST /chat/invoke`` against the
    # dynamic-agents service. Mirrors ``a2a_timeout_seconds`` for the
    # supervisor path. Per-task ``timeout_seconds`` overrides this.
    dynamic_agents_timeout_seconds: float = Field(default=300.0, gt=0)

    # Per-call HTTP timeout for the dynamic-agent preflight probe
    # (``GET /agents/{id}/probe``). Tight on purpose — preflight is
    # interactive and a slow probe wedges the editor form.
    dynamic_agents_preflight_timeout_seconds: float = Field(
        default=10.0, gt=0
    )

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
        "dynamic_agents_timeout_seconds",
        "dynamic_agents_preflight_timeout_seconds",
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

    # Global fallback HMAC secret for incoming webhooks. When a webhook
    # task has no per-task ``secret`` configured the router falls back
    # to this value so operators can rotate or supply secrets via a
    # single env var (``WEBHOOK_SECRET``) without editing every task.
    # Per-task secrets always win when both are configured.
    webhook_secret: str | None = None

    # IMP-07 — webhook replay protection.
    #
    # When > 0, signed webhooks must additionally carry an
    # ``X-Webhook-Timestamp`` header (Unix seconds, integer or float)
    # and the HMAC signature is computed over ``f"{timestamp}.{body}"``
    # rather than just ``body``. Requests whose timestamp is older
    # than ``webhook_replay_window_seconds`` (or in the future by more
    # than the same window, to allow modest clock skew) are rejected.
    #
    # Disabled by default (= 0) so existing GitHub-style senders that
    # only sign the body keep working. Operators flip this to e.g.
    # ``300`` (5 min) once their senders are updated to include the
    # timestamp header. See README.md for the signing contract.
    webhook_replay_window_seconds: int = Field(default=0, ge=0)

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

    # MongoDB persistence (REQUIRED).
    # Both ``mongodb_uri`` and ``mongodb_database`` must be set before
    # the service will start -- the lifespan in ``main.py`` calls
    # ``fatal_exit`` if either is missing or if the connection retry
    # loop exhausts ``mongodb_connect_max_attempts`` without success.
    # There is intentionally no in-memory fallback: silently running
    # on ephemeral stores would lose every task definition and run
    # record on the next restart, and production operators reliably
    # mis-diagnose that as "the scheduler broke".
    #
    # These stay as ``str | None`` at the Pydantic level (rather than
    # required fields) so tests that construct ``Settings()`` directly
    # -- especially unit tests that never go through the lifespan --
    # don't need to pass them in.
    mongodb_uri: str | None = None
    mongodb_database: str | None = None
    mongodb_collection: str = "autonomous_runs"

    # MongoDB collection that holds task definitions (the source of
    # truth for CRUD operations).
    mongodb_tasks_collection: str = "autonomous_tasks"

    # Connect-retry knobs used by main.py's lifespan. First connect
    # attempt happens immediately; subsequent attempts wait ``delay``
    # seconds between tries. ``ge=1`` keeps "never try" from being
    # silently legal via ``MONGODB_CONNECT_MAX_ATTEMPTS=0``.
    mongodb_connect_max_attempts: int = Field(default=3, ge=1)
    mongodb_connect_retry_delay_seconds: float = Field(default=2.0, gt=0)

    # IMP-16 — circuit breaker around the supervisor A2A call.
    #
    # Enabled by default because the failure mode it prevents
    # (every scheduled task burning its full retry budget against a
    # broken supervisor) is exactly the cascading-failure pattern
    # autonomous workloads cause. Operators can flip this off via
    # ``CIRCUIT_BREAKER_ENABLED=0`` if they ever need to.
    circuit_breaker_enabled: bool = True

    # How many *consecutive* post-retry failures trip the breaker.
    # Counted only after ``a2a_max_retries`` is exhausted, so a flaky
    # request that succeeds on retry leaves the breaker untouched.
    # Default of 5 trades a little extra failure-tolerance for fewer
    # false-positive trips on brief supervisor restarts.
    circuit_breaker_failure_threshold: int = Field(default=5, ge=1)

    # How long the breaker stays OPEN before letting a single trial
    # request through (HALF_OPEN). 30s is long enough that a crashed
    # supervisor has a real chance to come back, short enough that a
    # transient outage doesn't wedge scheduled runs for minutes.
    circuit_breaker_cooldown_seconds: float = Field(default=30.0, gt=0)

    @field_validator("circuit_breaker_cooldown_seconds")
    @classmethod
    def _reject_nonfinite_cb_cooldown(cls, v: float) -> float:
        # Same hardening as ``a2a_*`` knobs: ``inf`` would wedge the
        # breaker permanently OPEN, ``nan`` would compare false against
        # everything and silently disable the cooldown gate.
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        return v

    # IMP-13 — chat history publishing.
    #
    # When enabled, the scheduler writes each completed run as a
    # tagged conversation (``source: "autonomous"``) into the UI's
    # ``conversations`` + ``messages`` collections so operators can
    # see autonomous activity in the existing chat sidebar.
    #
    # Off by default: the UI's chat schema is owned by another
    # package, and writing into it is a cross-package contract that
    # an operator should opt into deliberately. When off, no Mongo
    # connection is opened against the chat database at all.
    chat_history_publish_enabled: bool = False

    # Owner email stamped on every autonomous-origin conversation /
    # message. The UI's chat list query filters by ``owner_id``,
    # ``sharing.shared_with``, etc.; the autonomous-only filter chip
    # bypasses that filter, so this address is mainly an audit-trail
    # marker rather than a real ACL anchor. Pick something clearly
    # synthetic so humans don't mistake it for a colleague.
    chat_history_owner_email: str = "autonomous@system"

    # Optional override for the database that holds the UI chat
    # collections. Defaults to ``mongodb_database`` so single-DB
    # deployments need no extra config; operators with a separate
    # logical DB for UI chat data can point this elsewhere without
    # affecting run-history persistence.
    chat_history_database: str | None = None

    # Collection names mirror the UI defaults from
    # ``ui/src/lib/mongodb.ts``. Exposed as settings so a CAIPE
    # deployment that has renamed them (rare) doesn't have to fork
    # this code to keep publishing working.
    chat_history_conversations_collection: str = "conversations"
    chat_history_messages_collection: str = "messages"

    # Webhook-context redaction switch (default OFF).
    # The autonomous agent's published prompt could otherwise contain
    # the entire raw webhook payload (e.g. a GitHub PR body, a
    # PagerDuty incident JSON) which the UI then renders to *any*
    # authenticated viewer, because chat-history rows tagged
    # ``source: 'autonomous'`` are read-accessible to all logged-in
    # users for audit visibility (see ``requireConversationAccess``).
    # Defaulting to OFF means an operator must opt in deliberately
    # before potentially-sensitive webhook bodies are mirrored into
    # broad-readable chat. With this off, the published prompt is
    # the bare ``task.prompt`` plus an opaque
    # ``Context: <redacted N keys>`` marker so debugging "did the
    # webhook fire?" is still possible without exposing payload
    # contents.
    chat_history_include_context: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
