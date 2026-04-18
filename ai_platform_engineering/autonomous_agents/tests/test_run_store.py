# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the in-memory ``RunStore`` implementation."""

import pytest

from autonomous_agents.models import TaskRun, TaskStatus
from autonomous_agents.services.run_store import InMemoryRunStore, RunStore


def _make_run(run_id: str, task_id: str = "t1", status: TaskStatus = TaskStatus.RUNNING) -> TaskRun:
    return TaskRun(run_id=run_id, task_id=task_id, task_name=f"task {task_id}", status=status)


def test_in_memory_store_satisfies_runstore_protocol():
    assert isinstance(InMemoryRunStore(), RunStore)


def test_invalid_maxlen_raises():
    with pytest.raises(ValueError):
        InMemoryRunStore(maxlen=0)
    with pytest.raises(ValueError):
        InMemoryRunStore(maxlen=-1)


async def test_record_and_list_all_returns_newest_first():
    store = InMemoryRunStore()
    await store.record(_make_run("r1"))
    await store.record(_make_run("r2"))
    await store.record(_make_run("r3"))

    runs = await store.list_all()
    assert [r.run_id for r in runs] == ["r3", "r2", "r1"]


async def test_record_upserts_by_run_id():
    store = InMemoryRunStore()
    await store.record(_make_run("r1", status=TaskStatus.RUNNING))

    updated = _make_run("r1", status=TaskStatus.SUCCESS)
    updated.response_preview = "ok"
    await store.record(updated)

    runs = await store.list_all()
    assert len(runs) == 1
    assert runs[0].status == TaskStatus.SUCCESS
    assert runs[0].response_preview == "ok"


async def test_list_by_task_filters_and_orders_newest_first():
    store = InMemoryRunStore()
    await store.record(_make_run("a1", task_id="alpha"))
    await store.record(_make_run("b1", task_id="beta"))
    await store.record(_make_run("a2", task_id="alpha"))
    await store.record(_make_run("a3", task_id="alpha"))

    alphas = await store.list_by_task("alpha")
    assert [r.run_id for r in alphas] == ["a3", "a2", "a1"]

    betas = await store.list_by_task("beta")
    assert [r.run_id for r in betas] == ["b1"]


async def test_list_by_task_returns_empty_for_unknown_task():
    store = InMemoryRunStore()
    await store.record(_make_run("r1", task_id="alpha"))
    assert await store.list_by_task("does-not-exist") == []


async def test_list_by_task_respects_limit():
    store = InMemoryRunStore()
    for i in range(5):
        await store.record(_make_run(f"a{i}", task_id="alpha"))

    limited = await store.list_by_task("alpha", limit=2)
    assert [r.run_id for r in limited] == ["a4", "a3"]


async def test_list_all_respects_limit():
    store = InMemoryRunStore()
    for i in range(5):
        await store.record(_make_run(f"r{i}"))

    limited = await store.list_all(limit=3)
    assert [r.run_id for r in limited] == ["r4", "r3", "r2"]


async def test_zero_or_negative_limit_returns_empty_list():
    store = InMemoryRunStore()
    await store.record(_make_run("r1"))
    assert await store.list_all(limit=0) == []
    assert await store.list_all(limit=-1) == []
    assert await store.list_by_task("t1", limit=0) == []


async def test_eviction_drops_oldest_first():
    store = InMemoryRunStore(maxlen=2)
    await store.record(_make_run("r1"))
    await store.record(_make_run("r2"))
    await store.record(_make_run("r3"))

    runs = await store.list_all()
    assert [r.run_id for r in runs] == ["r3", "r2"]


async def test_update_existing_run_does_not_trigger_eviction():
    """Re-recording an existing run_id must not push another out.

    The scheduler updates each run twice (RUNNING -> SUCCESS); this would
    halve effective capacity if updates were treated as inserts.
    """
    store = InMemoryRunStore(maxlen=2)
    await store.record(_make_run("r1"))
    await store.record(_make_run("r2"))

    updated_r1 = _make_run("r1", status=TaskStatus.SUCCESS)
    updated_r1.response_preview = "done"
    await store.record(updated_r1)

    runs = await store.list_all()
    assert {r.run_id for r in runs} == {"r1", "r2"}
    by_id = {r.run_id: r for r in runs}
    assert by_id["r1"].status == TaskStatus.SUCCESS
    assert by_id["r1"].response_preview == "done"
