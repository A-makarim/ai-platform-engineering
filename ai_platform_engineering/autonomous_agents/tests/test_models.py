# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for autonomous_agents models and task loader."""

import pydantic
import pytest

from autonomous_agents.models import (
    CronTrigger,
    IntervalTrigger,
    TaskDefinition,
    TaskStatus,
    TriggerType,
    WebhookTrigger,
)


def test_cron_trigger_type():
    trigger = CronTrigger(schedule="0 9 * * *")
    assert trigger.type == TriggerType.CRON
    assert trigger.schedule == "0 9 * * *"


def test_interval_trigger_type():
    trigger = IntervalTrigger(minutes=30)
    assert trigger.type == TriggerType.INTERVAL
    assert trigger.minutes == 30


def test_webhook_trigger_type():
    trigger = WebhookTrigger()
    assert trigger.type == TriggerType.WEBHOOK


def test_task_definition_cron():
    task = TaskDefinition(
        id="daily-scan",
        name="Daily Scan",
        agent="github",
        prompt="Scan for vulnerabilities",
        trigger=CronTrigger(schedule="0 9 * * 1-5"),
    )
    assert task.id == "daily-scan"
    assert task.enabled is True
    assert task.trigger.type == TriggerType.CRON


def test_task_definition_disabled_by_default_is_true():
    task = TaskDefinition(
        id="test",
        name="Test",
        agent="github",
        prompt="test prompt",
        trigger=IntervalTrigger(minutes=10),
    )
    assert task.enabled is True


def test_task_definition_can_be_disabled():
    task = TaskDefinition(
        id="test",
        name="Test",
        agent="github",
        prompt="test prompt",
        trigger=IntervalTrigger(hours=1),
        enabled=False,
    )
    assert task.enabled is False


def test_task_status_values():
    assert TaskStatus.PENDING == "pending"
    assert TaskStatus.RUNNING == "running"
    assert TaskStatus.SUCCESS == "success"
    assert TaskStatus.FAILED == "failed"


def test_webhook_trigger_optional_secret():
    trigger = WebhookTrigger(secret="my-secret")
    assert trigger.secret == "my-secret"

    trigger_no_secret = WebhookTrigger()
    assert trigger_no_secret.secret is None


# =============================================================================
# Per-task A2A overrides (IMP-02)
# =============================================================================

def test_task_definition_a2a_overrides_default_to_none():
    """No overrides specified → both fields are None so the scheduler
    falls back to Settings.a2a_timeout_seconds / a2a_max_retries.
    """
    task = TaskDefinition(
        id="test",
        name="Test",
        agent="github",
        prompt="x",
        trigger=CronTrigger(schedule="* * * * *"),
    )
    assert task.timeout_seconds is None
    assert task.max_retries is None


def test_task_definition_accepts_per_task_overrides():
    task = TaskDefinition(
        id="test",
        name="Test",
        agent="github",
        prompt="x",
        trigger=CronTrigger(schedule="* * * * *"),
        timeout_seconds=42.5,
        max_retries=5,
    )
    assert task.timeout_seconds == 42.5
    assert task.max_retries == 5


def test_task_definition_max_retries_zero_is_valid():
    """max_retries=0 is a meaningful value: 'best effort, do not retry'.
    The validator must allow it (only negative is rejected).
    """
    task = TaskDefinition(
        id="test",
        name="Test",
        agent="github",
        prompt="x",
        trigger=CronTrigger(schedule="* * * * *"),
        max_retries=0,
    )
    assert task.max_retries == 0


def test_task_definition_rejects_non_positive_timeout():
    for bad in (0, -1, -0.5):
        with pytest.raises(pydantic.ValidationError):
            TaskDefinition(
                id="test",
                name="Test",
                agent="github",
                prompt="x",
                trigger=CronTrigger(schedule="* * * * *"),
                timeout_seconds=bad,
            )


def test_task_definition_rejects_negative_max_retries():
    with pytest.raises(pydantic.ValidationError):
        TaskDefinition(
            id="test",
            name="Test",
            agent="github",
            prompt="x",
            trigger=CronTrigger(schedule="* * * * *"),
            max_retries=-1,
        )


def test_task_definition_rejects_inf_and_nan_timeout():
    """``timeout_seconds`` must reject ``inf`` / ``-inf`` / ``nan``.

    PyYAML parses ``.inf`` and ``.nan`` straight into float values, and
    Pydantic's ``gt=0`` constraint considers ``inf`` to satisfy ``> 0``.
    Without an explicit guard, ``timeout_seconds: .inf`` in config.yaml
    would propagate to httpx and silently break the per-attempt timeout
    at runtime.
    """
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(pydantic.ValidationError):
            TaskDefinition(
                id="test",
                name="Test",
                agent="github",
                prompt="x",
                trigger=CronTrigger(schedule="* * * * *"),
                timeout_seconds=bad,
            )
