# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the scheduler hot-reload helpers.

These exercise ``register_task`` / ``unregister_task`` directly without
spinning up a real APScheduler event loop -- we just inspect the
``get_scheduler().get_jobs()`` view after each call.

The CRUD endpoints in ``routes/tasks.py`` lean on these helpers so a
user can create, edit, or delete an autonomous task from the UI without
restarting the service. Anything that breaks idempotency or quietly
drops disabled tasks here would surface as a confusing UI bug ("I
deleted it but it still ran at 09:00").
"""

import pytest

from autonomous_agents.models import (
    CronTrigger,
    IntervalTrigger,
    TaskDefinition,
    WebhookTrigger,
)
from autonomous_agents.scheduler import (
    get_scheduler,
    register_task,
    register_tasks,
    unregister_task,
)


def _task(
    task_id: str = "t1",
    *,
    enabled: bool = True,
    trigger=None,
) -> TaskDefinition:
    return TaskDefinition(
        id=task_id,
        name=f"Task {task_id}",
        agent="github",
        prompt="hello",
        trigger=trigger or CronTrigger(schedule="0 9 * * *"),
        enabled=enabled,
    )


@pytest.fixture(autouse=True)
async def _fresh_scheduler():
    """Reset the module-level APScheduler singleton between tests.

    APScheduler's ``replace_existing=True`` only deduplicates against
    the active jobstore -- before ``start()`` is called, jobs land in
    a ``_pending_jobs`` list that does NOT honour replace semantics.
    To match the lifespan behaviour (scheduler is running by the time
    CRUD requests arrive), start the scheduler in paused mode so cron
    jobs don't actually fire during the test. The fixture must be
    async because ``AsyncIOScheduler.start()`` reaches for the
    currently-running event loop.
    """
    import autonomous_agents.scheduler as scheduler_mod

    scheduler_mod._scheduler = None
    sched = scheduler_mod.get_scheduler()
    sched.start(paused=True)
    yield
    if scheduler_mod._scheduler is not None and scheduler_mod._scheduler.running:
        scheduler_mod._scheduler.shutdown(wait=False)
    scheduler_mod._scheduler = None


@pytest.mark.asyncio
async def test_register_task_adds_cron_job():
    register_task(_task("cron-1", trigger=CronTrigger(schedule="*/5 * * * *")))

    jobs = get_scheduler().get_jobs()
    assert [j.id for j in jobs] == ["cron-1"]


@pytest.mark.asyncio
async def test_register_task_adds_interval_job():
    register_task(_task("int-1", trigger=IntervalTrigger(seconds=30)))

    jobs = get_scheduler().get_jobs()
    assert [j.id for j in jobs] == ["int-1"]


@pytest.mark.asyncio
async def test_register_task_skips_webhook_trigger():
    """Webhook tasks live in the /hooks router's registry, not in
    APScheduler. Registering one here would create a phantom job
    that never fires (no trigger schedule)."""
    register_task(_task("hook-1", trigger=WebhookTrigger()))

    assert get_scheduler().get_jobs() == []


@pytest.mark.asyncio
async def test_register_task_skips_disabled_task():
    """Disabled tasks must not be scheduled -- otherwise toggling
    ``enabled=false`` from the UI would have no operational effect
    until the next service restart."""
    register_task(_task("dis-1", enabled=False))

    assert get_scheduler().get_jobs() == []


@pytest.mark.asyncio
async def test_register_task_is_idempotent_for_same_id():
    """``replace_existing=True`` means re-registering the same id
    swaps the underlying job atomically rather than raising or
    creating a duplicate. The CRUD update path relies on this."""
    register_task(_task("t1", trigger=CronTrigger(schedule="0 9 * * *")))
    register_task(_task("t1", trigger=CronTrigger(schedule="0 18 * * *")))

    jobs = get_scheduler().get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "t1"


@pytest.mark.asyncio
async def test_register_task_replaces_trigger_on_re_register():
    """Beyond just "still one job", the new trigger spec must actually
    replace the old one -- otherwise edits via PUT would silently no-op."""
    register_task(_task("t1", trigger=CronTrigger(schedule="0 9 * * *")))
    first_trigger = get_scheduler().get_job("t1").trigger

    register_task(_task("t1", trigger=IntervalTrigger(minutes=15)))
    second_trigger = get_scheduler().get_job("t1").trigger

    # Concrete classes differ -- APScheduler swaps in the right type.
    assert type(first_trigger).__name__ == "CronTrigger"
    assert type(second_trigger).__name__ == "IntervalTrigger"


@pytest.mark.asyncio
async def test_unregister_task_removes_existing_job():
    register_task(_task("t1"))

    removed = unregister_task("t1")

    assert removed is True
    assert get_scheduler().get_jobs() == []


@pytest.mark.asyncio
async def test_unregister_task_returns_false_for_unknown_id():
    """Webhook-only tasks and never-registered ids are the common
    case for "no job to remove". The helper must not raise so the
    CRUD delete handler can call it unconditionally."""
    assert unregister_task("ghost") is False


@pytest.mark.asyncio
async def test_unregister_then_register_round_trip():
    """Sanity: a UI sequence of "delete then re-create" lands a fresh
    job rather than reviving a stale one or duplicating."""
    register_task(_task("t1"))
    assert unregister_task("t1") is True

    register_task(_task("t1", trigger=IntervalTrigger(hours=1)))

    jobs = get_scheduler().get_jobs()
    assert len(jobs) == 1
    assert jobs[0].id == "t1"


@pytest.mark.asyncio
async def test_register_tasks_bulk_keeps_scheduler_running():
    """The lifespan path calls ``register_tasks([...])`` once. Verify
    it both adds every cron/interval entry AND leaves the scheduler
    in a running state -- otherwise jobs would just sit in the
    jobstore and never fire."""
    tasks = [
        _task("cron-1", trigger=CronTrigger(schedule="0 * * * *")),
        _task("int-1", trigger=IntervalTrigger(minutes=5)),
        _task("hook-1", trigger=WebhookTrigger()),  # skipped, fine
        _task("dis-1", enabled=False),  # skipped, fine
    ]

    register_tasks(tasks)

    scheduler = get_scheduler()
    assert {j.id for j in scheduler.get_jobs()} == {"cron-1", "int-1"}
    assert scheduler.running is True


@pytest.mark.asyncio
async def test_register_tasks_does_not_double_start_running_scheduler():
    """A second bulk-register call (e.g. from a future "reload from
    YAML" admin button) must not crash -- APScheduler's ``start()``
    raises ``SchedulerAlreadyRunningError`` if called twice."""
    register_tasks([_task("t1")])
    # Second call should be a no-op for the start() side; jobs may
    # be replaced but the scheduler must not raise.
    register_tasks([_task("t1"), _task("t2")])

    scheduler = get_scheduler()
    assert {j.id for j in scheduler.get_jobs()} == {"t1", "t2"}
    assert scheduler.running is True
