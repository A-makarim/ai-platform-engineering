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
from autonomous_agents.services.chat_history import (
    ChatHistoryPublisher,
    NoopChatHistoryPublisher,
    _conversation_id_for_task,
)
from autonomous_agents.services.run_store import InMemoryRunStore, RunStore

logger = logging.getLogger("autonomous_agents")

_scheduler: AsyncIOScheduler | None = None
_run_store: RunStore | None = None
_chat_history_publisher: ChatHistoryPublisher | None = None


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


def get_chat_history_publisher() -> ChatHistoryPublisher:
    """Return the active :class:`ChatHistoryPublisher`.

    Defaults to a no-op publisher so unit tests that don't care
    about IMP-13 can keep exercising :func:`execute_task` without
    setting anything up. The lifespan hook injects the real one
    when ``CHAT_HISTORY_PUBLISH_ENABLED`` is on.
    """
    global _chat_history_publisher
    if _chat_history_publisher is None:
        _chat_history_publisher = NoopChatHistoryPublisher()
    return _chat_history_publisher


def set_chat_history_publisher(publisher: ChatHistoryPublisher) -> None:
    """Inject the active :class:`ChatHistoryPublisher` — called from the FastAPI lifespan."""
    global _chat_history_publisher
    _chat_history_publisher = publisher


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


async def _publish_safely(
    publisher: ChatHistoryPublisher,
    run: TaskRun,
    task: TaskDefinition,
    context: dict[str, Any] | None,
    *,
    response: str | None,
    error: str | None,
    agent: str | None,
    prompt_override: str | None = None,
) -> None:
    """Surface the run in the UI chat history -- best effort.

    Same contract as :func:`_record_safely`: chat-history publishing
    is an observability feature, not part of the source of truth.
    A misconfigured or unavailable chat database must never propagate
    out and either abort the task or 500 the webhook that fired it.
    Log loudly, swallow the exception, return.

    ``prompt_override``: ad-hoc messages typed into an autonomous chat
    thread (POST /tasks/{id}/message — spec #099 Story 2 / Iteration A)
    pass the typed text here so the chat thread shows what the operator
    actually said, not the static task prompt.

    Note: prompt construction lives *inside* the try block on purpose
    -- a non-JSON-serialisable webhook context would otherwise raise
    out of ``execute_task``'s finally clause, contradicting the "chat
    publishing must never abort a task" goal (Copilot review on PR
    #10).
    """
    try:
        prompt = _prompt_for_publish(task, context, prompt_override=prompt_override)
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
            f"[{run.task_id}] Failed to publish run {run.run_id} to chat history "
            f"(status={run.status}): {exc}"
        )


async def execute_task(
    task: TaskDefinition,
    context: dict[str, Any] | None = None,
    *,
    prompt_override: str | None = None,
) -> TaskRun:
    """Run a single task, record the result, and return the TaskRun.

    Public entry point used by:
      * APScheduler (cron/interval fires) — uses ``task.prompt`` as-is.
      * Manual ``POST /tasks/{id}/run`` — same.
      * Webhook fires — same; webhook payload becomes ``context``.
      * Per-message chat (spec #099 Story 2 / Iteration A) —
        ``POST /tasks/{id}/message`` passes the typed text as
        ``prompt_override`` so the supervisor sees the operator's
        ad-hoc question instead of the canonical task prompt.

    The contextId on the wire is always the task's deterministic
    UUIDv5 so the supervisor's checkpointer keeps a single
    conversation thread regardless of which entry point fired the
    run. That's what makes the per-task chat thread feel continuous
    across typed and scheduled messages.
    """
    run_id = str(uuid.uuid4())
    # Pre-compute the deterministic per-task conversation id so it lands
    # in ``autonomous_runs`` from the very first RUNNING write -- the
    # UI can then deep-link from a run row to ``/chat/<id>`` as soon
    # as the run appears, even before the terminal state is recorded.
    # Spec #099 FR-006 / AD-002: one chat thread per task, not per run.
    conversation_id = _conversation_id_for_task(task.id)
    # The prompt actually sent to the supervisor — used for both the
    # A2A call and the chat-history "run_request" message so the chat
    # thread shows what the operator typed (not the static task prompt)
    # for ad-hoc messages.
    effective_prompt = prompt_override if prompt_override is not None else task.prompt
    run = TaskRun(
        run_id=run_id,
        task_id=task.id,
        task_name=task.name,
        status=TaskStatus.RUNNING,
        conversation_id=conversation_id,
    )

    store = get_run_store()
    # Persist the RUNNING state so observers (UI, CLI) can see in-flight
    # work, not only completed runs. Failure here MUST NOT abort the
    # task — see _record_safely.
    await _record_safely(store, run)

    log_prefix = "ad-hoc msg" if prompt_override is not None else "scheduled run"
    logger.info(f"[{task.id}] Starting {log_prefix} {run_id}")
    response_text: str | None = None
    error_text: str | None = None
    try:
        response = await invoke_agent(
            prompt=effective_prompt,
            task_id=task.id,
            agent=task.agent,
            llm_provider=task.llm_provider,
            context=context,
            timeout_seconds=task.timeout_seconds,
            max_retries=task.max_retries,
        )
        response_text = response
        run.status = TaskStatus.SUCCESS
        run.response_preview = response[:500]
        logger.info(f"[{task.id}] {log_prefix.capitalize()} {run_id} succeeded. Preview: {response[:120]}...")
    except Exception as e:
        error_text = str(e)
        run.status = TaskStatus.FAILED
        run.error = error_text
        logger.error(f"[{task.id}] {log_prefix.capitalize()} {run_id} failed: {e}")
    finally:
        run.finished_at = datetime.now(timezone.utc)
        # Persist the terminal state — RunStore.record is upsert by
        # run_id, so this updates the same document/entry rather than
        # appending a duplicate. Again wrapped to keep store outages
        # from masking the real task outcome.
        await _record_safely(store, run)
        # IMP-13: surface the run in the UI chat sidebar. Done after
        # the RunStore write so a slow/flaky chat database can never
        # delay the authoritative run-history record. The publisher
        # is a no-op when ``CHAT_HISTORY_PUBLISH_ENABLED`` is off so
        # this is essentially free in the default config.
        # We pass ``effective_prompt`` (not ``task.prompt``) so the
        # chat thread shows the typed message, not the static task
        # prompt, for ad-hoc messages.
        await _publish_safely(
            get_chat_history_publisher(),
            run,
            task,
            context,
            response=response_text,
            error=error_text,
            agent=task.agent,
            prompt_override=prompt_override,
        )

    return run


def _prompt_for_publish(
    task: TaskDefinition,
    context: dict[str, Any] | None,
    *,
    prompt_override: str | None = None,
) -> str:
    """Reconstruct the user-visible prompt for chat-history publishing.

    Three modes:

    * **Ad-hoc message** (``prompt_override`` set): the operator typed a
      free-form message into the autonomous chat thread. Show it as-is
      — the typed text is what should appear in the user-message
      bubble, not the static task prompt.
    * **Scheduled / manual run with no webhook context**: show
      ``task.prompt`` unmodified.
    * **Webhook-fired run**: mirror the augmentation that
      ``services.a2a_client.invoke_agent`` applies before sending to
      the supervisor — append a ``Context: <redacted>`` marker by
      default, inline the JSON only when
      ``CHAT_HISTORY_INCLUDE_CONTEXT=true``.

    Webhook payloads frequently contain internal/customer data
    (incident bodies, PR descriptions, customer ids). The chat
    history is read-accessible to *any* authenticated UI user via
    ``requireConversationAccess`` (PR #10 Codex P1 review), so
    inlining raw context would be a data-exposure regression.
    """
    # Ad-hoc typed message wins outright — it IS the user message.
    if prompt_override is not None:
        return prompt_override

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
