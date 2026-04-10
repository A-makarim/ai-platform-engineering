"""APScheduler setup — registers cron and interval tasks at startup."""

import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger as APSCronTrigger
from apscheduler.triggers.interval import IntervalTrigger as APSIntervalTrigger

from autonomous_agents.models import (
    CronTrigger,
    IntervalTrigger,
    TaskDefinition,
    TaskRun,
    TaskStatus,
    TriggerType,
)
from autonomous_agents.services.a2a_client import invoke_agent

logger = logging.getLogger("autonomous_agents")

# In-memory run history (last 500 runs); deque provides O(1) left-drop
_run_history: deque[TaskRun] = deque(maxlen=500)
_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def get_run_history() -> list[TaskRun]:
    return list(reversed(_run_history))


async def _execute_task(task: TaskDefinition, context: dict[str, Any] | None = None) -> TaskRun:
    """Run a single task, record the result, and return the TaskRun."""
    run_id = str(uuid.uuid4())
    run = TaskRun(run_id=run_id, task_id=task.id, task_name=task.name, status=TaskStatus.RUNNING)
    _run_history.append(run)

    logger.info(f"[{task.id}] Starting run {run_id}")
    try:
        response = await invoke_agent(
            prompt=task.prompt,
            task_id=task.id,
            agent=task.agent,
            llm_provider=task.llm_provider,
            context=context,
        )
        run.status = TaskStatus.SUCCESS
        run.response_preview = response[:500]
        logger.info(f"[{task.id}] Run {run_id} succeeded. Preview: {response[:120]}...")
    except Exception as e:
        run.status = TaskStatus.FAILED
        run.error = str(e)
        logger.error(f"[{task.id}] Run {run_id} failed: {e}")
    finally:
        run.finished_at = datetime.now(timezone.utc)

    return run


def register_tasks(tasks: list[TaskDefinition]) -> None:
    """Register all cron and interval tasks with APScheduler.

    Webhook tasks are handled by the /hooks router and do not need scheduling.
    """
    scheduler = get_scheduler()

    for task in tasks:
        trigger = task.trigger

        if trigger.type == TriggerType.WEBHOOK:
            logger.info(f"[{task.id}] Webhook task — will fire on POST /hooks/{task.id}")
            continue

        if trigger.type == TriggerType.CRON:
            if not isinstance(trigger, CronTrigger):
                logger.warning(f"[{task.id}] Expected CronTrigger, got {type(trigger).__name__} — skipping")
                continue
            aps_trigger = APSCronTrigger.from_crontab(trigger.schedule, timezone="UTC")
            logger.info(f"[{task.id}] Scheduling cron: {trigger.schedule}")

        elif trigger.type == TriggerType.INTERVAL:
            if not isinstance(trigger, IntervalTrigger):
                logger.warning(f"[{task.id}] Expected IntervalTrigger, got {type(trigger).__name__} — skipping")
                continue
            aps_trigger = APSIntervalTrigger(
                seconds=trigger.seconds or 0,
                minutes=trigger.minutes or 0,
                hours=trigger.hours or 0,
            )
            logger.info(f"[{task.id}] Scheduling interval: {trigger.seconds}s / {trigger.minutes}m / {trigger.hours}h")

        else:
            logger.warning(f"[{task.id}] Unknown trigger type '{trigger.type}' — skipping")
            continue

        scheduler.add_job(
            _execute_task,
            trigger=aps_trigger,
            args=[task],
            id=task.id,
            name=task.name,
            replace_existing=True,
            misfire_grace_time=60,
        )

    scheduler.start()
    logger.info(f"Scheduler started with {len(scheduler.get_jobs())} job(s)")


async def fire_webhook_task(task: TaskDefinition, context: dict[str, Any]) -> TaskRun:
    """Immediately execute a webhook-triggered task and return the completed run."""
    return await _execute_task(task, context=context)
