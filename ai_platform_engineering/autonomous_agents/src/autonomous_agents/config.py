"""Configuration settings for Autonomous Agents service."""

import json
from functools import lru_cache
from typing import Any, Self

from pydantic import AliasChoices, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_cors_string(raw: str | None) -> list[str]:
    """Parse ``CORS_ORIGINS`` / constructor value into a list of origins.

    Accepts: empty (no CORS), a JSON array string, or a comma-separated list.
    pydantic-settings must *not* bind this field as ``list[str]`` directly:
    an empty env var becomes ``""`` and the settings source calls
    ``json.loads("")`` before any field validator runs, which crashes startup.
    """
    if raw is None:
        return []
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("["):
        parsed = json.loads(s)
        if not isinstance(parsed, list):
            raise ValueError("CORS_ORIGINS JSON must be a JSON array of strings")
        return [str(x).strip() for x in parsed if str(x).strip()]
    return [origin.strip() for origin in s.split(",") if origin.strip()]


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

    # CORS — stored as a raw string so Docker ``CORS_ORIGINS=`` (empty) does
    # not trip pydantic-settings' JSON decode for ``list[str]``. Expose the
    # parsed list via the ``cors_origins`` property (same name as before).
    cors_origins_raw: str = Field(
        default="",
        validation_alias=AliasChoices("CORS_ORIGINS", "AUTONOMOUS_CORS_ORIGINS"),
    )
    _cors_origins: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _legacy_cors_constructor_kwarg(cls, data: Any) -> Any:
        # Unit tests and callers use ``Settings(cors_origins=[...])`` /
        # ``Settings(cors_origins="http://a,http://b")`` — translate to raw.
        if not isinstance(data, dict) or "cors_origins" not in data:
            return data
        co = data.pop("cors_origins")
        if isinstance(co, list):
            data["cors_origins_raw"] = ",".join(str(x) for x in co if str(x)) if co else ""
        elif isinstance(co, str):
            data["cors_origins_raw"] = co
        return data

    @model_validator(mode="after")
    def _materialize_cors_origins(self) -> Self:
        # IMP-05: reject ``*`` with allow_credentials=True (see main.py).
        parsed = _parse_cors_string(self.cors_origins_raw)
        if any(origin.strip() == "*" for origin in parsed):
            raise ValueError(
                "cors_origins=['*'] is unsafe with allow_credentials=True; "
                "list each allowed origin explicitly (e.g. "
                "['http://localhost:3000','https://app.example.com'])"
            )
        self._cors_origins = parsed
        return self

    @property
    def cors_origins(self) -> list[str]:
        return self._cors_origins

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
