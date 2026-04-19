# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :class:`InMemoryTaskStore`.

These exercise the protocol contract — Mongo-backed behaviour lives in
``test_mongo_task_store.py`` so the in-memory tests run without any
external dependency (not even ``mongomock-motor``).
"""

import pytest

from autonomous_agents.models import (
    CronTrigger,
    IntervalTrigger,
    TaskDefinition,
    WebhookTrigger,
)
from autonomous_agents.services.task_store import (
    InMemoryTaskStore,
    TaskAlreadyExistsError,
    TaskNotFoundError,
    TaskStore,
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


def test_in_memory_task_store_implements_protocol():
    """Catches accidental signature drift between Protocol and impl."""
    assert isinstance(InMemoryTaskStore(), TaskStore)


async def test_create_and_get_roundtrip():
    store = InMemoryTaskStore()
    task = _task("t1")

    created = await store.create(task)

    assert created == task
    fetched = await store.get("t1")
    assert fetched == task


async def test_get_returns_none_for_missing_task():
    """Distinct from raising — callers (UI 404, scheduler skip) need a
    cheap "is it there?" check that doesn't throw."""
    assert await InMemoryTaskStore().get("ghost") is None


async def test_create_rejects_duplicate_id():
    """Duplicate ``id`` must raise a typed error so the API layer can
    map it to HTTP 409 without string-matching the message."""
    store = InMemoryTaskStore()
    await store.create(_task("t1"))

    with pytest.raises(TaskAlreadyExistsError) as exc:
        await store.create(_task("t1"))

    assert exc.value.task_id == "t1"


async def test_create_failure_leaves_store_unchanged():
    """Atomicity contract: a failed create must not corrupt prior state."""
    store = InMemoryTaskStore()
    original = _task("t1")
    await store.create(original)

    with pytest.raises(TaskAlreadyExistsError):
        # Try to overwrite via create — same id, different prompt.
        await store.create(_task("t1"))

    # The original prompt should still be intact.
    fetched = await store.get("t1")
    assert fetched is not None
    assert fetched.prompt == original.prompt


async def test_list_all_preserves_insertion_order():
    """Iterating in insertion order keeps the UI list stable across
    refreshes — no surprise reshuffles for the operator."""
    store = InMemoryTaskStore()
    for i in range(5):
        await store.create(_task(f"t{i}"))

    listed = await store.list_all()

    assert [t.id for t in listed] == [f"t{i}" for i in range(5)]


async def test_list_all_returns_snapshot_not_live_view():
    """Mutating the returned list must not change the store's state.
    Otherwise a UI handler holding the result while a delete is in
    flight could silently desync."""
    store = InMemoryTaskStore()
    await store.create(_task("t1"))

    listed = await store.list_all()
    listed.clear()

    # Store is untouched.
    assert (await store.get("t1")) is not None
    assert len(await store.list_all()) == 1


async def test_update_replaces_in_place():
    store = InMemoryTaskStore()
    await store.create(_task("t1"))
    new_version = TaskDefinition(
        id="t1",
        name="Renamed",
        agent="argocd",
        prompt="updated prompt",
        trigger=IntervalTrigger(minutes=5),
        enabled=False,
    )

    returned = await store.update("t1", new_version)

    assert returned == new_version
    fetched = await store.get("t1")
    assert fetched is not None
    assert fetched.name == "Renamed"
    assert fetched.agent == "argocd"
    assert fetched.enabled is False
    assert fetched.trigger.type.value == "interval"


async def test_update_rejects_id_mismatch():
    """Path id and body id must agree — silently honouring a mismatch
    would let the UI rename tasks on PUT, breaking every URL bookmark."""
    store = InMemoryTaskStore()
    await store.create(_task("t1"))

    with pytest.raises(ValueError, match="does not match"):
        await store.update("t1", _task("t2"))

    # And no half-applied state on failure.
    assert await store.get("t2") is None
    assert (await store.get("t1")) is not None


async def test_update_raises_when_target_missing():
    """Operate-by-id requires the target to exist — silently upserting
    on update would mask a stale UI showing a deleted task."""
    store = InMemoryTaskStore()

    with pytest.raises(TaskNotFoundError) as exc:
        await store.update("ghost", _task("ghost"))

    assert exc.value.task_id == "ghost"


async def test_delete_removes_task():
    store = InMemoryTaskStore()
    await store.create(_task("t1"))
    await store.create(_task("t2"))

    await store.delete("t1")

    assert await store.get("t1") is None
    assert (await store.get("t2")) is not None
    assert {t.id for t in await store.list_all()} == {"t2"}


async def test_delete_raises_when_target_missing():
    """A no-op delete usually masks a race the caller should know
    about (two operators deleting the same task simultaneously, a
    stale UI list, etc.). Force them to handle it."""
    store = InMemoryTaskStore()

    with pytest.raises(TaskNotFoundError) as exc:
        await store.delete("ghost")

    assert exc.value.task_id == "ghost"


async def test_round_trip_preserves_all_trigger_types():
    """Sanity: every TriggerType variant survives create -> get without
    losing data — important because the UI form reuses these models."""
    store = InMemoryTaskStore()
    cron_task = _task("cron-1", trigger=CronTrigger(schedule="*/5 * * * *"))
    interval_task = _task("interval-1", trigger=IntervalTrigger(seconds=30))
    webhook_task = _task("webhook-1", trigger=WebhookTrigger(secret="sssh"))

    for t in (cron_task, interval_task, webhook_task):
        await store.create(t)

    fetched_cron = await store.get("cron-1")
    fetched_interval = await store.get("interval-1")
    fetched_webhook = await store.get("webhook-1")

    assert fetched_cron is not None and fetched_cron.trigger == cron_task.trigger
    assert fetched_interval is not None and fetched_interval.trigger == interval_task.trigger
    assert fetched_webhook is not None and fetched_webhook.trigger == webhook_task.trigger
