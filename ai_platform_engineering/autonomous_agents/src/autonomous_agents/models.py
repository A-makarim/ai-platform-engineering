"""Pydantic models for Autonomous Agents service."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


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
    type: TriggerType = TriggerType.CRON
    schedule: str = Field(..., description="Cron expression e.g. '0 9 * * *'")


class IntervalTrigger(BaseModel):
    type: TriggerType = TriggerType.INTERVAL
    seconds: int | None = None
    minutes: int | None = None
    hours: int | None = None

    @model_validator(mode="after")
    def require_positive_interval(self) -> "IntervalTrigger":
        total = (self.seconds or 0) + (self.minutes or 0) * 60 + (self.hours or 0) * 3600
        if total <= 0:
            raise ValueError("IntervalTrigger requires at least one of seconds, minutes, or hours to be a positive integer")
        return self


class WebhookTrigger(BaseModel):
    type: TriggerType = TriggerType.WEBHOOK
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


# =============================================================================
# Webhook payload
# =============================================================================

class WebhookPayload(BaseModel):
    """Generic webhook payload — passed as context to the agent prompt."""
    source: str | None = None
    event: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
