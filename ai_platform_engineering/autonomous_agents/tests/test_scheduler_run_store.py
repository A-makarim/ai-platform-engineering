# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the scheduler <-> RunStore wiring.

These tests exercise the public effect: when ``_execute_task`` runs to
completion (success or failure), the configured ``RunStore`` ends up
holding a single, terminal-state ``TaskRun`` with the run_id returned
by the call.

The A2A side (``invoke_agent``) is mocked so the tests have no network
dependency and don't need a live supervisor.
"""

from unittest.mock import AsyncMock, patch

import pytest

from autonomous_agents.models import CronTrigger, TaskDefinition, TaskStatus
from autonomous_agents.scheduler import _execute_task, get_run_store, set_run_store
from autonomous_agents.services.run_store import InMemoryRunStore


@pytest.fixture(autouse=True)
def _reset_scheduler_run_store():
    """Restore the scheduler's module-level run_store after each test.

    Without this, leakage between tests would mask both real bugs
    (e.g. a test sees data left by another) and false failures
    (e.g. a test sees a Mongo store from a previous suite).
    """
    import autonomous_agents.scheduler as scheduler_mod

    original = scheduler_mod._run_store
    scheduler_mod._run_store = None
    yield
    scheduler_mod._run_store = original


@pytest.fixture
def store() -> InMemoryRunStore:
    s = InMemoryRunStore(maxlen=10)
    set_run_store(s)
    return s


@pytest.fixture
def task() -> TaskDefinition:
    return TaskDefinition(
        id="test-task",
        name="Test Task",
        agent="github",
        prompt="echo hello",
        trigger=CronTrigger(schedule="0 9 * * *"),
    )


def test_get_run_store_lazily_creates_in_memory_default():
    """If the lifespan hook never injected a store, scheduler functions
    must still work — they fall back to a fresh InMemoryRunStore."""
    s = get_run_store()
    assert isinstance(s, InMemoryRunStore)


def test_set_run_store_replaces_active_store():
    custom = InMemoryRunStore(maxlen=7)
    set_run_store(custom)
    assert get_run_store() is custom


async def test_execute_task_records_running_then_success(store: InMemoryRunStore, task: TaskDefinition):
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="hello world"),
    ):
        run = await _execute_task(task)

    assert run.status == TaskStatus.SUCCESS
    assert run.response_preview == "hello world"

    # record() is upsert by run_id, so we expect exactly one entry
    # despite TWO calls (one at start, one at finish).
    runs = await store.list_all()
    assert len(runs) == 1
    assert runs[0].run_id == run.run_id
    assert runs[0].status == TaskStatus.SUCCESS
    assert runs[0].finished_at is not None


async def test_execute_task_records_failure_with_error_message(store: InMemoryRunStore, task: TaskDefinition):
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        run = await _execute_task(task)

    assert run.status == TaskStatus.FAILED

    runs = await store.list_all()
    assert len(runs) == 1
    persisted = runs[0]
    assert persisted.run_id == run.run_id
    assert persisted.status == TaskStatus.FAILED
    assert persisted.error == "boom"
    assert persisted.finished_at is not None


async def test_running_state_is_visible_before_completion(store: InMemoryRunStore, task: TaskDefinition):
    """While invoke_agent is in flight, the RUNNING entry must already
    be queryable from the store. This is the whole point of recording
    twice (start + end) — observers see in-flight work."""

    snapshot: list[TaskStatus] = []

    async def slow_agent(*args, **kwargs):
        # Capture what the store holds while we're "running".
        rs = await store.list_all()
        if rs:
            snapshot.append(rs[0].status)
        return "done"

    with patch("autonomous_agents.scheduler.invoke_agent", new=AsyncMock(side_effect=slow_agent)):
        await _execute_task(task)

    assert snapshot == [TaskStatus.RUNNING]
    runs = await store.list_all()
    assert runs[0].status == TaskStatus.SUCCESS


async def test_execute_task_returns_same_run_object_as_persisted(store: InMemoryRunStore, task: TaskDefinition):
    """The returned TaskRun is the same instance as the one in the store
    — callers (e.g. webhooks router) rely on this for synchronous
    response payloads."""
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="x"),
    ):
        run = await _execute_task(task)

    persisted = (await store.list_all())[0]
    assert persisted is run
