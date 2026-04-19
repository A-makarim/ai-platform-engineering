"""Pydantic models for Autonomous Agents service."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TriggerType(str, Enum):
    CRON = "cron"
    WEBHOOK = "webhook"
    INTERVAL = "interval"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


# =============================================================================
# Trigger definitions
# =============================================================================

class CronTrigger(BaseModel):
    type: Literal[TriggerType.CRON] = TriggerType.CRON
    schedule: str = Field(..., description="Cron expression e.g. '0 9 * * *'")


class IntervalTrigger(BaseModel):
    type: Literal[TriggerType.INTERVAL] = TriggerType.INTERVAL
    seconds: int | None = None
    minutes: int | None = None
    hours: int | None = None

    @model_validator(mode="after")
    def require_positive_interval(self) -> "IntervalTrigger":
        invalid = [name for name, val in [("seconds", self.seconds), ("minutes", self.minutes), ("hours", self.hours)] if val is not None and val <= 0]
        if invalid:
            raise ValueError(f"IntervalTrigger fields must be positive integers: {', '.join(invalid)}")
        if not any([self.seconds, self.minutes, self.hours]):
            raise ValueError("IntervalTrigger requires at least one of seconds, minutes, or hours to be a positive integer")
        return self


class WebhookTrigger(BaseModel):
    type: Literal[TriggerType.WEBHOOK] = TriggerType.WEBHOOK
    secret: str | None = Field(None, description="Optional HMAC secret for payload validation")


Trigger = CronTrigger | IntervalTrigger | WebhookTrigger


# =============================================================================
# Task definition (loaded from YAML)
# =============================================================================

class TaskDefinition(BaseModel):
    id: str = Field(..., description="Unique task identifier")
    name: str = Field(..., description="Human-readable task name")
    description: str | None = None
    agent: str = Field(..., description="Target agent name (must match CAIPE agent id)")
    prompt: str = Field(..., description="Prompt sent to the agent when this task fires")
    trigger: CronTrigger | IntervalTrigger | WebhookTrigger = Field(..., discriminator="type")
    llm_provider: str | None = Field(None, description="Override global LLM provider for this task")
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Optional per-task overrides for the supervisor A2A call. When None,
    # the service-wide defaults from Settings (A2A_TIMEOUT_SECONDS /
    # A2A_MAX_RETRIES) apply. Useful for slow-running synthesis prompts
    # (raise the timeout) or for "best-effort, don't burn quota" tasks
    # (force max_retries=0).
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description="Override A2A_TIMEOUT_SECONDS for this task (seconds, > 0).",
    )
    max_retries: int | None = Field(
        default=None,
        ge=0,
        description="Override A2A_MAX_RETRIES for this task (>= 0; 0 disables retries).",
    )

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_must_be_finite(cls, v: float | None) -> float | None:
        # Pydantic's ``gt=0`` constraint accepts ``float('inf')`` and ``nan``,
        # and PyYAML happily parses ``.inf`` / ``.nan`` from config.yaml.
        # Either would silently break the httpx timeout at runtime, so reject
        # both at load time. ``Settings`` has the same guard for the global
        # default — keep them in lockstep.
        if v is None:
            return v
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("timeout_seconds must be a finite number")
        return v


# =============================================================================
# Task run records (in-memory, can be backed by DB later)
# =============================================================================

class TaskRun(BaseModel):
    run_id: str
    task_id: str
    task_name: str
    status: TaskStatus
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    response_preview: str | None = None
    error: str | None = None
    # IMP-13: id of the chat-history conversation that mirrors this
    # run, when publishing is enabled. Lets the UI deep-link from a
    # run row to ``/chat/<conversation_id>``. Optional and stable per
    # ``run_id`` (UUID5-derived) so the field is safe to leave unset
    # for runs from before publishing was turned on.
    conversation_id: str | None = None


# =============================================================================
# Webhook payload
# =============================================================================

class WebhookPayload(BaseModel):
    """Generic webhook payload — passed as context to the agent prompt."""
    source: str | None = None
    event: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
