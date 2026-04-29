"""APScheduler setup — registers cron and interval tasks at startup.

Also exposes single-task ``register_task`` / ``unregister_task`` helpers so
the CRUD endpoints can hot-reload the scheduler without bouncing the
service.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

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
    TriggerInstance,
    TriggerSource,
    TriggerType,
)
from autonomous_agents.services.a2a_client import invoke_agent_streaming
from autonomous_agents.services.chat_history import (
    ChatHistoryPublisher,
    NoopChatHistoryPublisher,
    _conversation_id_for_task,
)
from autonomous_agents.services.mongo import RunStore
from autonomous_agents.services.trigger_dedup import (
    derive_scheduled_dedupe_key,
)

logger = logging.getLogger("autonomous_agents")


@runtime_checkable
class TriggerInstanceStore(Protocol):
    """Minimal IMP-20 dedup contract the scheduler depends on.

    Decoupled from :class:`MongoService` so unit tests can inject a
    tiny in-memory fake without pulling in mongomock_motor for every
    scheduler test that doesn't care about the dedup path. The
    lifespan injects the real Mongo-backed implementation.
    """

    async def try_acquire_trigger(
        self, instance: TriggerInstance
    ) -> tuple[TriggerInstance, bool]: ...

    async def attach_run_to_trigger(
        self, trigger_id: str, run_id: str
    ) -> None: ...


# Resolver returning the live TaskDefinition for a given id. Kept as a
# callable rather than the TaskStore Protocol so tests can inject a
# bare lambda; the scheduler module already avoids importing TaskStore
# directly to keep its dependency graph small.
TaskResolver = Callable[[str], Awaitable[TaskDefinition | None]]


_scheduler: AsyncIOScheduler | None = None
_run_store: RunStore | None = None
_chat_history_publisher: ChatHistoryPublisher | None = None
_trigger_store: TriggerInstanceStore | None = None
_task_resolver: TaskResolver | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def get_run_store() -> RunStore:
    """Return the active :class:`RunStore`.

    The lifespan hook in ``main.py`` injects the MongoDB-backed store
    before any handler runs. Unit tests that exercise scheduler
    functions without going through the lifespan MUST inject a fake
    via :func:`set_run_store` first; we refuse to silently lazy-build
    an in-memory store because that would hide a real misconfiguration
    in production (MongoDB required -- see ``main.lifespan``).
    """
    if _run_store is None:
        raise RuntimeError(
            "RunStore not initialized -- call set_run_store(...) "
            "(the FastAPI lifespan does this automatically after "
            "connecting to MongoDB)"
        )
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


def set_trigger_store(store: TriggerInstanceStore | None) -> None:
    """Inject the IMP-20 :class:`TriggerInstanceStore`.

    ``None`` disables dedup for callers that route through the
    scheduler-side helper (``_acquire_scheduled_trigger`` below); the
    routes layer reaches into the store directly so it short-circuits
    on its own when the kill-switch is active.
    """
    global _trigger_store
    _trigger_store = store


def get_trigger_store() -> TriggerInstanceStore | None:
    return _trigger_store


def set_task_resolver(resolver: TaskResolver | None) -> None:
    """Inject the live-task lookup used by :func:`_execute_scheduled`.

    ``None`` is legal for tests that exercise :func:`execute_task`
    directly without going through the APScheduler wrapper. The
    lifespan binds this to ``TaskStore.get`` so cron/interval fires
    always see the latest persisted definition rather than the one
    captured at ``register_task`` time.
    """
    global _task_resolver
    _task_resolver = resolver


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
) -> None:
    """Surface the run in the UI chat history -- best effort.

    Same contract as :func:`_record_safely`: chat-history publishing
    is an observability feature, not part of the source of truth.
    A misconfigured or unavailable chat database must never propagate
    out and either abort the task or 500 the webhook that fired it.
    Log loudly, swallow the exception, return.

    Note: prompt construction lives *inside* the try block on purpose
    -- a non-JSON-serialisable webhook context would otherwise raise
    out of ``execute_task``'s finally clause, contradicting the "chat
    publishing must never abort a task" goal (Copilot review on PR
    #10).
    """
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
            f"[{run.task_id}] Failed to publish run {run.run_id} to chat history "
            f"(status={run.status}): {exc}"
        )


async def execute_task(
    task: TaskDefinition,
    context: dict[str, Any] | None = None,
    *,
    trigger_id: str | None = None,
) -> TaskRun:
    """Run a single task, record the result, and return the TaskRun.

    Public entry point used both by APScheduler (cron/interval) and by
    the routes layer (manual trigger, webhook). Keeping this public is
    intentional — it's part of the contract with the FastAPI handlers
    that drive ad-hoc execution. Don't add a leading underscore back.

    ``trigger_id`` (IMP-20) is the dedup-record id the caller acquired
    before invoking this function; we stamp it on the ``TaskRun`` and
    -- best effort -- write the back-reference onto the trigger row so
    the audit chain ``trigger -> run -> conversation`` is walkable in
    one Mongo query. ``None`` is the legacy contract and remains valid
    for callers that don't go through dedup yet.
    """
    run_id = str(uuid.uuid4())
    # Pre-compute the deterministic per-task conversation id so it lands
    # in ``autonomous_runs`` from the very first RUNNING write -- the
    # UI can then deep-link from a run row to ``/chat/<id>`` as soon
    # as the run appears, even before the terminal state is recorded.
    # Spec #099 FR-006 / AD-002: one chat thread per task, not per run.
    conversation_id = _conversation_id_for_task(task.id)
    run = TaskRun(
        run_id=run_id,
        task_id=task.id,
        task_name=task.name,
        status=TaskStatus.RUNNING,
        conversation_id=conversation_id,
        trigger_id=trigger_id,
    )

    store = get_run_store()
    # Persist the RUNNING state so observers (UI, CLI) can see in-flight
    # work, not only completed runs. Failure here MUST NOT abort the
    # task — see _record_safely.
    await _record_safely(store, run)

    # Best-effort back-reference on the trigger row. Same swallow-all
    # contract as ``_record_safely``: a flaky Mongo here must never
    # abort the run. The store handles its own logging.
    if trigger_id is not None:
        trigger_store = get_trigger_store()
        if trigger_store is not None:
            try:
                await trigger_store.attach_run_to_trigger(trigger_id, run_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[%s] attach_run_to_trigger swallowed for trigger=%s: %s",
                    task.id, trigger_id, exc,
                )

    logger.info(f"[{task.id}] Starting run {run_id}")
    response_text: str | None = None
    error_text: str | None = None
    try:
        # Phase B (spec #099 Story 2): use the streaming variant so we
        # capture every supervisor A2A event (execution_plan_update,
        # tool_notification_*, final_result, etc.) — persisted on the
        # TaskRun and replayed by the UI synthesiser so past scheduled
        # fires render with the same rich plan + tools + timeline a
        # typed chat reply gets.
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
            f"[{task.id}] Run {run_id} succeeded "
            f"({len(events)} events, {len(response)} chars). "
            f"Preview: {response[:120]}..."
        )
    except Exception as e:
        error_text = str(e)
        run.status = TaskStatus.FAILED
        run.error = error_text
        logger.error(f"[{task.id}] Run {run_id} failed: {e}")
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
    """Reconstruct the user-visible prompt for chat-history publishing.

    Mirrors the augmentation that ``services.a2a_client.invoke_agent``
    applies before sending to the supervisor: when a webhook supplies
    a context payload, the actual prompt the agent saw is
    ``f"{prompt}\n\nContext:\n{json}"``. Showing the same string in
    chat keeps the conversation honest — otherwise a webhook-triggered
    run would look like the bare prompt fired with no context, and
    debugging "why did the agent do X?" becomes much harder.

    Webhook payloads frequently contain internal/customer data
    (incident bodies, PR descriptions, customer ids). The chat
    history is read-accessible to *any* authenticated UI user via
    ``requireConversationAccess`` (PR #10 Codex P1 review), so
    inlining the raw context into the published prompt would be a
    data-exposure regression. We default to a redacted marker
    (``Context: <redacted N keys>``) and only inline the payload
    when the operator explicitly opts in via
    ``CHAT_HISTORY_INCLUDE_CONTEXT=true``.
    """
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
        source = TriggerSource.CRON
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
        source = TriggerSource.INTERVAL
        logger.info(f"[{task.id}] Scheduling interval: {trigger.seconds}s / {trigger.minutes}m / {trigger.hours}h")

    else:
        logger.warning(f"[{task.id}] Unknown trigger type '{trigger.type}' — skipping")
        return

    # IMP-20: register the dedup-aware wrapper instead of execute_task
    # directly. The wrapper re-reads the task from the store on each
    # fire so live edits take effect without re-registering. We pass
    # task.id (not task) so a stale captured-by-closure definition can
    # never be silently used after a CRUD update.
    #
    # ``max_instances=1`` + ``coalesce=True`` are defence in depth on
    # top of the Mongo unique index: APScheduler won't even submit a
    # second concurrent run of the same job, so the unique-index race
    # only kicks in for the multi-replica case (which is the scenario
    # IMP-15 will fully address).
    get_scheduler().add_job(
        _execute_scheduled,
        trigger=aps_trigger,
        args=[task.id, source],
        id=task.id,
        name=task.name,
        replace_existing=True,
        misfire_grace_time=60,
        max_instances=1,
        coalesce=True,
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


async def fire_webhook_task(
    task: TaskDefinition,
    context: dict[str, Any],
    *,
    trigger_id: str | None = None,
) -> TaskRun:
    """Immediately execute a webhook-triggered task and return the completed run.

    ``trigger_id`` (IMP-20) is forwarded to :func:`execute_task` so the
    resulting :class:`TaskRun` cross-references the dedup row written
    by the webhook router.
    """
    return await execute_task(task, context=context, trigger_id=trigger_id)


async def _execute_scheduled(task_id: str, source: TriggerSource) -> None:
    """APScheduler entry point: dedup, then ``execute_task``.

    This is the wrapper that APScheduler actually invokes (not
    :func:`execute_task` directly). It does three things in order:

    1. Resolves the *current* :class:`TaskDefinition` via the injected
       resolver. Re-reading on every fire means a task that's been
       disabled or deleted between registration and fire time
       short-circuits cleanly instead of running against stale state.
       (``register_task`` does already replace the job on update, but
       a same-tick race with a CRUD edit can still slip through.)
    2. Acquires a :class:`TriggerInstance` keyed on
       ``{source}:{task_id}:{rounded_fire_time}``. Two replicas (or
       APScheduler's own multi-submit when a slow run blocks the
       schedule) firing the same tick converge on the same key and
       only one insert wins; the loser logs and returns.
    3. Calls :func:`execute_task` with the trigger id so the run row
       carries the cross-reference.

    All three steps swallow store-side exceptions and fall back to
    "execute the task anyway" so a degraded Mongo never silently
    drops scheduled fires. Dedup is a correctness *enhancement*; the
    base contract is "the cron tick fires the task".
    """
    resolver = _task_resolver
    if resolver is None:
        # Defensive: should never happen in production -- the lifespan
        # always injects a resolver before starting the scheduler.
        # Fall back to a one-shot warning so a misconfigured test
        # surfaces instead of silently no-oping.
        logger.warning(
            "[%s] _execute_scheduled invoked without a task resolver -- skipping",
            task_id,
        )
        return

    try:
        task = await resolver(task_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[%s] _execute_scheduled: task resolver raised %s -- skipping fire",
            task_id, exc,
        )
        return

    if task is None:
        logger.info(
            "[%s] _execute_scheduled: task no longer exists -- skipping fire",
            task_id,
        )
        return
    if not task.enabled:
        logger.info(
            "[%s] _execute_scheduled: task disabled -- skipping fire",
            task_id,
        )
        return

    fire_time = datetime.now(timezone.utc)
    dedupe_key = derive_scheduled_dedupe_key(task_id, source, fire_time)

    trigger_id: str | None = None
    trigger_store = get_trigger_store()
    if trigger_store is not None:
        instance = TriggerInstance(
            trigger_id=str(uuid.uuid4()),
            task_id=task_id,
            dedupe_key=dedupe_key,
            source=source,
            scheduled_for=fire_time.replace(microsecond=0),
        )
        try:
            stored, acquired = await trigger_store.try_acquire_trigger(instance)
        except Exception as exc:  # noqa: BLE001
            # Mongo failure on the dedup write must not block the run --
            # we'd rather risk a duplicate fire (which the operator
            # can spot in run history) than miss a legitimate cron.
            logger.warning(
                "[%s] _execute_scheduled: try_acquire_trigger failed (%s) -- "
                "executing without dedup protection",
                task_id, exc,
            )
            stored, acquired = instance, True
        if not acquired:
            logger.info(
                "[%s] _execute_scheduled: duplicate fire for %s "
                "(dedupe_key=%s, original_run=%s) -- skipping",
                task_id, source.value, dedupe_key, stored.run_id,
            )
            return
        trigger_id = stored.trigger_id

    await execute_task(task, trigger_id=trigger_id)
