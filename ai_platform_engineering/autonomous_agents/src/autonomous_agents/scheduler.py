"""APScheduler setup and task execution helpers for autonomous agents."""

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
from autonomous_agents.services.a2a_client import invoke_agent_streaming
from autonomous_agents.services.chat_history import (
    ChatHistoryPublisher,
    NoopChatHistoryPublisher,
    _conversation_id_for_task,
)
from autonomous_agents.services.mongo import RunStore

logger = logging.getLogger("autonomous_agents")

UTC = "UTC"

_scheduler: AsyncIOScheduler | None = None
_run_store: RunStore | None = None
_chat_history_publisher: ChatHistoryPublisher | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=UTC)
    return _scheduler


def get_run_store() -> RunStore:
    """Return injected RunStore (set by lifespan or tests)."""
    if _run_store is None:
        raise RuntimeError(
            "RunStore not initialized -- call set_run_store(...) "
            "(the FastAPI lifespan does this automatically after connecting to MongoDB)"
        )
    return _run_store


def set_run_store(store: RunStore) -> None:
    global _run_store
    _run_store = store


def get_chat_history_publisher() -> ChatHistoryPublisher:
    """Return injected publisher; defaults to no-op."""
    global _chat_history_publisher
    if _chat_history_publisher is None:
        _chat_history_publisher = NoopChatHistoryPublisher()
    return _chat_history_publisher


def set_chat_history_publisher(publisher: ChatHistoryPublisher) -> None:
    global _chat_history_publisher
    _chat_history_publisher = publisher


async def _record_safely(store: RunStore, run: TaskRun) -> None:
    """Persist run without letting storage failures abort execution."""
    try:
        await store.record(run)
    except Exception as exc:
        logger.error(
            "[%s] Failed to persist run %s (status=%s): %s",
            run.task_id,
            run.run_id,
            run.status,
            exc,
        )


async def _publish_safely(
    publisher: ChatHistoryPublisher,
    run: TaskRun,
    task: TaskDefinition,
    context: dict[str, Any] | None,
    *,
    response: str | None,
    error: str | None,
    agent: str | None,
) -> None:
    """Publish run to chat history without surfacing publisher failures."""
    try:
        prompt = _prompt_for_publish(task, context)
        await publisher.publish_run(
            run,
            prompt=prompt,
            response=response,
            error=error,
            agent=agent,
            conversation_id=run.conversation_id,
        )
    except Exception as exc:
        logger.error(
            "[%s] Failed to publish run %s to chat history (status=%s): %s",
            run.task_id,
            run.run_id,
            run.status,
            exc,
        )


async def execute_task(
    task: TaskDefinition,
    context: dict[str, Any] | None = None,
) -> TaskRun:
    """Run one task, persist run state transitions, and return TaskRun."""
    run_id = str(uuid.uuid4())
    conversation_id = _conversation_id_for_task(task.id)

    run = TaskRun(
        run_id=run_id,
        task_id=task.id,
        task_name=task.name,
        status=TaskStatus.RUNNING,
        conversation_id=conversation_id,
    )

    store = get_run_store()
    await _record_safely(store, run)

    logger.info("[%s] Starting run %s", task.id, run_id)

    response_text: str | None = None
    error_text: str | None = None

    try:
        response, events = await invoke_agent_streaming(
            prompt=task.prompt,
            task_id=task.id,
            agent=task.agent,
            llm_provider=task.llm_provider,
            context=context,
            timeout_seconds=task.timeout_seconds,
        )
        response_text = response
        run.status = TaskStatus.SUCCESS
        run.response_preview = response[:500]
        run.response_full = response
        run.events = events

        logger.info(
            "[%s] Run %s succeeded (%d events, %d chars). Preview: %s...",
            task.id,
            run_id,
            len(events),
            len(response),
            response[:120],
        )
    except Exception as exc:
        error_text = str(exc)
        run.status = TaskStatus.FAILED
        run.error = error_text
        logger.error("[%s] Run %s failed: %s", task.id, run_id, exc)
    finally:
        run.finished_at = datetime.now(timezone.utc)
        await _record_safely(store, run)
        await _publish_safely(
            get_chat_history_publisher(),
            run,
            task,
            context,
            response=response_text,
            error=error_text,
            agent=task.agent,
        )

    return run


def _prompt_for_publish(
    task: TaskDefinition,
    context: dict[str, Any] | None,
) -> str:
    """Build prompt string displayed in chat-history publishing."""
    if not context:
        return task.prompt

    from autonomous_agents.config import get_settings

    if not get_settings().chat_history_include_context:
        return f"{task.prompt}\n\nContext: <redacted {len(context)} keys>"

    import json as _json

    try:
        rendered = _json.dumps(context, indent=2, default=str)
    except (TypeError, ValueError):
        rendered = f"<unserialisable context: {len(context)} keys>"

    return f"{task.prompt}\n\nContext:\n{rendered}"


def register_task(task: TaskDefinition) -> None:
    """Register one task in APScheduler (or unschedule if not applicable)."""
    if not task.enabled:
        unregister_task(task.id)
        logger.info("[%s] Disabled — not scheduling (any prior job removed)", task.id)
        return

    trigger = task.trigger

    if trigger.type == TriggerType.WEBHOOK:
        unregister_task(task.id)
        logger.info(
            "[%s] Webhook task — handled by /hooks router, not APScheduler",
            task.id,
        )
        return

    if trigger.type == TriggerType.CRON:
        if not isinstance(trigger, CronTrigger):
            logger.warning(
                "[%s] Expected CronTrigger, got %s — skipping",
                task.id,
                type(trigger).__name__,
            )
            return
        aps_trigger = APSCronTrigger.from_crontab(trigger.schedule, timezone=UTC)
        logger.info("[%s] Scheduling cron: %s", task.id, trigger.schedule)

    elif trigger.type == TriggerType.INTERVAL:
        if not isinstance(trigger, IntervalTrigger):
            logger.warning(
                "[%s] Expected IntervalTrigger, got %s — skipping",
                task.id,
                type(trigger).__name__,
            )
            return
        aps_trigger = APSIntervalTrigger(
            seconds=trigger.seconds or 0,
            minutes=trigger.minutes or 0,
            hours=trigger.hours or 0,
        )
        logger.info(
            "[%s] Scheduling interval: %ss / %sm / %sh",
            task.id,
            trigger.seconds,
            trigger.minutes,
            trigger.hours,
        )

    else:
        logger.warning("[%s] Unknown trigger type '%s' — skipping", task.id, trigger.type)
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
    """Remove task from scheduler if present."""
    scheduler = get_scheduler()
    try:
        scheduler.remove_job(task_id)
        logger.info("[%s] Removed from scheduler", task_id)
        return True
    except JobLookupError:
        return False


def register_tasks(tasks: list[TaskDefinition]) -> None:
    """Register tasks, then start scheduler once if needed."""
    for task in tasks:
        register_task(task)

    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
    logger.info("Scheduler started with %d job(s)", len(scheduler.get_jobs()))


async def fire_webhook_task(task: TaskDefinition, context: dict[str, Any]) -> TaskRun:
    """Execute webhook-triggered task immediately."""
    return await execute_task(task, context=context)