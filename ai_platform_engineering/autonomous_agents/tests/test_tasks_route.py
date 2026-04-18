# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the /tasks router run-history endpoints.

These tests focus on the contract between the router and the
``RunStore`` abstraction — specifically, the ``limit`` arguments
the router passes through. We don't need a FastAPI ``TestClient``
here: the router functions are plain ``async def`` callables, so
we can ``await`` them directly with a stub store.
"""

from datetime import datetime, timezone

import pytest

from autonomous_agents.models import CronTrigger, TaskDefinition, TaskRun, TaskStatus
from autonomous_agents.routes import tasks as tasks_route
from autonomous_agents.routes.tasks import (
    _MAX_TASK_RUNS,
    get_task_runs,
    list_all_runs,
    set_registered_tasks,
)


class _RecordingStore:
    """RunStore stub that captures the ``limit`` it was invoked with.

    Lets each test assert on the exact call shape the router used
    without standing up a real backend.
    """

    def __init__(self, runs: list[TaskRun] | None = None) -> None:
        self._runs = runs or []
        self.list_by_task_calls: list[tuple[str, int]] = []
        self.list_all_calls: list[int] = []

    async def record(self, run: TaskRun) -> None:  # pragma: no cover — unused
        self._runs.append(run)

    async def list_by_task(self, task_id: str, limit: int = 100) -> list[TaskRun]:
        self.list_by_task_calls.append((task_id, limit))
        # Filter + cap so tests can also assert on returned data.
        matching = [r for r in self._runs if r.task_id == task_id]
        return matching[:limit]

    async def list_all(self, limit: int = 500) -> list[TaskRun]:
        self.list_all_calls.append(limit)
        return self._runs[:limit]


def _make_run(run_id: str, task_id: str = "t1") -> TaskRun:
    return TaskRun(
        run_id=run_id,
        task_id=task_id,
        task_name=f"task {task_id}",
        status=TaskStatus.SUCCESS,
        started_at=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
def _reset_router_state(monkeypatch):
    """Restore module-level state the router caches."""
    original_tasks = list(tasks_route._registered_tasks)
    yield
    set_registered_tasks(original_tasks)


@pytest.fixture
def _swap_run_store(monkeypatch):
    """Patch ``get_run_store`` for the route module so tests inject
    a stub without touching the scheduler's global state."""

    def _apply(store):
        monkeypatch.setattr(tasks_route, "get_run_store", lambda: store)
        return store

    return _apply


async def test_get_task_runs_passes_max_task_runs_limit(_swap_run_store):
    """Regression: the router must pass an explicit limit, not rely on
    RunStore.list_by_task's protocol default of 100. Pre-fix this
    truncated history for any task with more than 100 past runs."""
    store = _swap_run_store(_RecordingStore([_make_run(f"r{i}") for i in range(120)]))
    set_registered_tasks([
        TaskDefinition(
            id="t1",
            name="t1",
            agent="github",
            prompt="x",
            trigger=CronTrigger(schedule="0 9 * * *"),
        )
    ])

    runs = await get_task_runs("t1")

    assert store.list_by_task_calls == [("t1", _MAX_TASK_RUNS)]
    assert _MAX_TASK_RUNS >= 500, "raise this guard if the cap shrinks"
    assert len(runs) == 120


async def test_get_task_runs_404_when_unknown_task_and_no_history(_swap_run_store):
    """Existing behaviour: a 404 should still fire when the task has
    neither a registered definition nor any historical runs."""
    from fastapi import HTTPException

    _swap_run_store(_RecordingStore())
    set_registered_tasks([])

    with pytest.raises(HTTPException) as exc:
        await get_task_runs("ghost")
    assert exc.value.status_code == 404


async def test_get_task_runs_returns_history_for_removed_tasks(_swap_run_store):
    """Existing behaviour: if a task is no longer in config.yaml but
    its runs are still in the store, the endpoint must still return
    them rather than 404. Codifies the intent behind the existing
    check, so a future refactor can't silently regress it."""
    store = _swap_run_store(_RecordingStore([_make_run("old", task_id="removed")]))
    set_registered_tasks([])

    runs = await get_task_runs("removed")

    assert len(runs) == 1
    assert store.list_by_task_calls == [("removed", _MAX_TASK_RUNS)]


async def test_list_all_runs_uses_default_limit(_swap_run_store):
    """``/runs`` accepts no params today, so it should hit the store
    with no override and rely on the protocol default (500)."""
    store = _swap_run_store(_RecordingStore([_make_run("r1")]))
    set_registered_tasks([])

    runs = await list_all_runs()

    assert len(runs) == 1
    assert store.list_all_calls == [500]
