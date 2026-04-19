# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the scheduler <-> MongoDBService wiring.

These tests exercise the public effect: when ``execute_task`` runs to
completion (success or failure), the configured ``MongoDBService`` ends up
holding a single, terminal-state ``TaskRun`` with the run_id returned
by the call.

The A2A side (``invoke_agent``) is mocked so the tests have no network
dependency and don't need a live supervisor.
"""

from unittest.mock import AsyncMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from autonomous_agents.models import CronTrigger, TaskDefinition, TaskStatus
from autonomous_agents.scheduler import (
    execute_task,
    get_persistence_service,
    set_persistence_service,
)
from autonomous_agents.services.mongo import MongoDBService


@pytest.fixture(autouse=True)
def _reset_scheduler_run_store():
    """Restore the scheduler's module-level persistence service after each test.

    Without this, leakage between tests would mask both real bugs
    (e.g. a test sees data left by another) and false failures
    (e.g. a test sees a Mongo store from a previous suite).
    """
    import autonomous_agents.scheduler as scheduler_mod

    original = scheduler_mod._mongo_service
    scheduler_mod._mongo_service = None
    yield
    scheduler_mod._mongo_service = original


@pytest.fixture
def store() -> MongoDBService:
    s = MongoDBService(AsyncMongoMockClient(), database_name="test_db")
    set_persistence_service(s)
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


def test_get_run_store_raises_before_initialization():
    with pytest.raises(RuntimeError, match="MongoDBService not initialized"):
        get_persistence_service()


def test_set_run_store_replaces_active_store():
    custom = MongoDBService(AsyncMongoMockClient(), database_name="test_db")
    set_persistence_service(custom)
    assert get_persistence_service() is custom


async def test_execute_task_records_running_then_success(store: MongoDBService, task: TaskDefinition):
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="hello world"),
    ):
        run = await execute_task(task)

    assert run.status == TaskStatus.SUCCESS
    assert run.response_preview == "hello world"

    # record() is upsert by run_id, so we expect exactly one entry
    # despite TWO calls (one at start, one at finish).
    runs = await store.list_runs()
    assert len(runs) == 1
    assert runs[0].run_id == run.run_id
    assert runs[0].status == TaskStatus.SUCCESS
    assert runs[0].finished_at is not None


async def test_execute_task_records_failure_with_error_message(store: MongoDBService, task: TaskDefinition):
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        run = await execute_task(task)

    assert run.status == TaskStatus.FAILED

    runs = await store.list_runs()
    assert len(runs) == 1
    persisted = runs[0]
    assert persisted.run_id == run.run_id
    assert persisted.status == TaskStatus.FAILED
    assert persisted.error == "boom"
    assert persisted.finished_at is not None


async def test_running_state_is_visible_before_completion(store: MongoDBService, task: TaskDefinition):
    """While invoke_agent is in flight, the RUNNING entry must already
    be queryable from the store. This is the whole point of recording
    twice (start + end) — observers see in-flight work."""

    snapshot: list[TaskStatus] = []

    async def slow_agent(*args, **kwargs):
        # Capture what the store holds while we're "running".
        rs = await store.list_runs()
        if rs:
            snapshot.append(rs[0].status)
        return "done"

    with patch("autonomous_agents.scheduler.invoke_agent", new=AsyncMock(side_effect=slow_agent)):
        await execute_task(task)

    assert snapshot == [TaskStatus.RUNNING]
    runs = await store.list_runs()
    assert runs[0].status == TaskStatus.SUCCESS


async def test_execute_task_returns_same_run_data_as_persisted(store: MongoDBService, task: TaskDefinition):
    """Mongo-backed persistence round-trips to a fresh TaskRun object,
    but the persisted payload must match what execute_task returned."""
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="x"),
    ):
        run = await execute_task(task)

    persisted = (await store.list_runs())[0]
    assert persisted.run_id == run.run_id
    assert persisted.status == run.status
    assert persisted.response_preview == run.response_preview


class _FlakyStore:
    """Persistence service stub that always raises on run writes.

    Implements the same run-write surface as MongoDBService so the
    scheduler treats it identically. Counts ``record_run`` invocations so
    tests can assert both start- and end-of-run persistence attempts
    were made.
    """

    def __init__(self) -> None:
        self.record_calls = 0

    async def record_run(self, run):
        self.record_calls += 1
        raise RuntimeError("simulated store outage")

    async def list_runs(self, limit: int = 500):  # pragma: no cover — unused here
        return []

    async def list_runs_by_task(self, task_id: str, limit: int = 100):  # pragma: no cover
        return []


async def test_run_store_failure_does_not_abort_task(task: TaskDefinition, caplog):
    """Regression: a broken RunStore must not bubble out of execute_task.

    Before this fix the very first ``await store.record(run)`` ran
    outside any try/except, so a transient Mongo failure would crash
    the scheduled job entirely — and, worse, surface as a 500 on the
    webhook router whose handler awaits the same coroutine.
    """
    set_persistence_service(_FlakyStore())

    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="ok"),
    ):
        run = await execute_task(task)

    assert run.status == TaskStatus.SUCCESS
    assert run.response_preview == "ok"
    assert run.finished_at is not None


async def test_run_store_failure_is_logged_at_error_level(task: TaskDefinition, caplog):
    """Operators must still see store outages — silent swallow would be worse than the crash."""
    flaky = _FlakyStore()
    set_persistence_service(flaky)

    with caplog.at_level("ERROR", logger="autonomous_agents"):
        with patch(
            "autonomous_agents.scheduler.invoke_agent",
            new=AsyncMock(return_value="ok"),
        ):
            await execute_task(task)

    # Two record attempts (start + finish), both should have logged.
    assert flaky.record_calls == 2
    error_messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert sum("Failed to persist run" in msg for msg in error_messages) == 2


async def test_run_store_failure_during_finalization_still_returns_completed_run(
    task: TaskDefinition,
):
    """Even if the *terminal* record() blows up in the finally-block,
    the caller still gets back a fully-populated TaskRun — important
    because the webhook router echoes this object straight back to
    the HTTP client."""
    set_persistence_service(_FlakyStore())

    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="hello"),
    ):
        run = await execute_task(task)

    assert run.status == TaskStatus.SUCCESS
    assert run.response_preview == "hello"
    assert run.finished_at is not None
