# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for autonomous_agents models and task loader."""

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
