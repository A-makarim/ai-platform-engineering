"""APScheduler setup — registers cron and interval tasks at startup.

Also exposes single-task ``register_task`` / ``unregister_task`` helpers so
the CRUD endpoints can hot-reload the scheduler without bouncing the
service.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from apscheduler.jobstores.base import JobLookupError
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
from autonomous_agents.services.run_store import InMemoryRunStore, RunStore

logger = logging.getLogger("autonomous_agents")

_scheduler: AsyncIOScheduler | None = None
_run_store: RunStore | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def get_run_store() -> RunStore:
    """Return the active :class:`RunStore`.

    Lazily falls back to an :class:`InMemoryRunStore` if the lifespan
    hook hasn't injected one yet (e.g. when scheduler functions are
    exercised directly from unit tests).
    """
    global _run_store
    if _run_store is None:
        _run_store = InMemoryRunStore()
    return _run_store


def set_run_store(store: RunStore) -> None:
    """Inject the active :class:`RunStore` — called from the FastAPI lifespan."""
    global _run_store
    _run_store = store


async def _record_safely(store: RunStore, run: TaskRun) -> None:
    """Persist ``run`` and swallow store-side exceptions.

    Run-history persistence is observability, not the source of truth
    for whether a task ran. A flaky MongoDB or transient network blip
    must never abort task execution or surface a 500 on the webhook
    that triggered the run. We log loudly so the failure is still
    visible to operators, then return so the scheduler keeps marching.
    """
    try:
        await store.record(run)
    except Exception as exc:
        logger.error(
            f"[{run.task_id}] Failed to persist run {run.run_id} "
            f"(status={run.status}): {exc}"
        )


async def execute_task(task: TaskDefinition, context: dict[str, Any] | None = None) -> TaskRun:
    """Run a single task, record the result, and return the TaskRun.

    Public entry point used both by APScheduler (cron/interval) and by
    the routes layer (manual trigger, webhook). Keeping this public is
    intentional — it's part of the contract with the FastAPI handlers
    that drive ad-hoc execution. Don't add a leading underscore back.
    """
    run_id = str(uuid.uuid4())
    run = TaskRun(run_id=run_id, task_id=task.id, task_name=task.name, status=TaskStatus.RUNNING)

    store = get_run_store()
    # Persist the RUNNING state so observers (UI, CLI) can see in-flight
    # work, not only completed runs. Failure here MUST NOT abort the
    # task — see _record_safely.
    await _record_safely(store, run)

    logger.info(f"[{task.id}] Starting run {run_id}")
    try:
        response = await invoke_agent(
            prompt=task.prompt,
            task_id=task.id,
            agent=task.agent,
            llm_provider=task.llm_provider,
            context=context,
            timeout_seconds=task.timeout_seconds,
            max_retries=task.max_retries,
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
        # Persist the terminal state — RunStore.record is upsert by
        # run_id, so this updates the same document/entry rather than
        # appending a duplicate. Again wrapped to keep store outages
        # from masking the real task outcome.
        await _record_safely(store, run)

    return run


def register_task(task: TaskDefinition) -> None:
    """Register a single cron / interval task with APScheduler.

    Idempotent: ``replace_existing=True`` means re-registering the same
    ``task.id`` (e.g. on update via the CRUD API) atomically replaces
    the prior job and any in-flight run completes against the new
    definition only on its *next* trigger fire.

    Webhook-only tasks are no-ops here — webhooks have their own
    router-side registry. Disabled tasks are *actively unscheduled*
    here so flipping ``enabled=false`` from the UI on an existing
    cron/interval task immediately stops it firing instead of leaving
    a zombie job until the next service restart (PR #5 review,
    Copilot+Codex P1).
    """
    if not task.enabled:
        # ``unregister_task`` is a no-op when no job exists, so this
        # is safe for tasks that were never scheduled in the first
        # place (newly-created disabled tasks, webhook tasks, etc.).
        unregister_task(task.id)
        logger.info(f"[{task.id}] Disabled — not scheduling (any prior job removed)")
        return

    trigger = task.trigger

    if trigger.type == TriggerType.WEBHOOK:
        # Trigger-type swap from cron/interval -> webhook: detach the
        # old APScheduler job. Same idempotent contract as the
        # disabled-task branch above.
        unregister_task(task.id)
        logger.info(f"[{task.id}] Webhook task — handled by /hooks router, not APScheduler")
        return

    if trigger.type == TriggerType.CRON:
        if not isinstance(trigger, CronTrigger):
            logger.warning(f"[{task.id}] Expected CronTrigger, got {type(trigger).__name__} — skipping")
            return
        aps_trigger = APSCronTrigger.from_crontab(trigger.schedule, timezone="UTC")
        logger.info(f"[{task.id}] Scheduling cron: {trigger.schedule}")

    elif trigger.type == TriggerType.INTERVAL:
        if not isinstance(trigger, IntervalTrigger):
            logger.warning(f"[{task.id}] Expected IntervalTrigger, got {type(trigger).__name__} — skipping")
            return
        aps_trigger = APSIntervalTrigger(
            seconds=trigger.seconds or 0,
            minutes=trigger.minutes or 0,
            hours=trigger.hours or 0,
        )
        logger.info(f"[{task.id}] Scheduling interval: {trigger.seconds}s / {trigger.minutes}m / {trigger.hours}h")

    else:
        logger.warning(f"[{task.id}] Unknown trigger type '{trigger.type}' — skipping")
        return

    get_scheduler().add_job(
        execute_task,
        trigger=aps_trigger,
        args=[task],
        id=task.id,
        name=task.name,
        replace_existing=True,
        misfire_grace_time=60,
    )


def unregister_task(task_id: str) -> bool:
    """Remove ``task_id`` from APScheduler if present.

    Returns ``True`` if a job was removed, ``False`` if no such job
    existed (e.g. a webhook-only task, a disabled task that was never
    scheduled, or a stale id from a duplicate UI delete). Returning a
    bool instead of raising lets the CRUD endpoint be idempotent
    without an extra "does it exist?" round-trip.
    """
    scheduler = get_scheduler()
    try:
        scheduler.remove_job(task_id)
        logger.info(f"[{task_id}] Removed from scheduler")
        return True
    except JobLookupError:
        # Not an error: webhook tasks and disabled tasks are never
        # added, so a "missing" job on delete is the common case.
        return False


def register_tasks(tasks: list[TaskDefinition]) -> None:
    """Bulk-register all cron and interval tasks, then start the scheduler.

    Called once from the FastAPI lifespan with the YAML-seeded task
    list. Subsequent CRUD-driven changes go through
    :func:`register_task` / :func:`unregister_task` directly so the
    scheduler is never restarted at runtime.
    """
    for task in tasks:
        register_task(task)

    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
    logger.info(f"Scheduler started with {len(scheduler.get_jobs())} job(s)")


async def fire_webhook_task(task: TaskDefinition, context: dict[str, Any]) -> TaskRun:
    """Immediately execute a webhook-triggered task and return the completed run."""
    return await execute_task(task, context=context)
