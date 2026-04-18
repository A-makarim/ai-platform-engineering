# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Run history persistence layer.

Provides a small Protocol-based abstraction so the scheduler can record
``TaskRun`` outcomes without caring whether they live in memory or in a
backing store such as MongoDB.

The default :class:`InMemoryRunStore` preserves the legacy
``deque(maxlen=500)`` behaviour from the original scheduler, so
development environments require zero external infrastructure.

Concrete persistent implementations (e.g. MongoDB) live alongside this
module and plug in via :func:`get_run_store` once configured.
"""

from collections import deque
from typing import Protocol, runtime_checkable

from autonomous_agents.models import TaskRun


@runtime_checkable
class RunStore(Protocol):
    """Async, append-mostly store for :class:`TaskRun` records.

    Implementations MUST be safe to call concurrently from the scheduler
    event loop. :meth:`record` is upsert-by-``run_id`` so the scheduler
    can call it once when a run starts (status=RUNNING) and again when
    it finishes (status=SUCCESS|FAILED) without the store needing a
    separate "update" path.
    """

    async def record(self, run: TaskRun) -> None:
        """Insert ``run`` if new, otherwise replace the existing record with the same ``run_id``."""
        ...

    async def list_by_task(self, task_id: str, limit: int = 100) -> list[TaskRun]:
        """Return runs for ``task_id`` newest first, capped at ``limit``."""
        ...

    async def list_all(self, limit: int = 500) -> list[TaskRun]:
        """Return runs across all tasks newest first, capped at ``limit``."""
        ...


class InMemoryRunStore:
    """Bounded in-memory :class:`RunStore` — the default when no DB is configured.

    Preserves legacy behaviour: the most recent ``maxlen`` runs across
    *all* tasks are retained; older runs are silently evicted FIFO.
    Suitable for development and single-replica deployments where losing
    history on restart is acceptable.

    Thread/task safety: assumes a single asyncio event loop driver
    (which the FastAPI + APScheduler setup guarantees). The internal
    state is plain dict/deque; no locking is needed because every
    mutating call is awaited and runs to completion before yielding.
    """

    def __init__(self, maxlen: int = 500) -> None:
        if maxlen <= 0:
            raise ValueError("maxlen must be a positive integer")
        self._maxlen = maxlen
        # Insertion order of run_ids; deque gives O(1) left-eviction.
        self._order: deque[str] = deque()
        # run_id -> TaskRun lookup. Keeping a separate dict means
        # update-in-place is O(1) without scanning the deque.
        self._runs: dict[str, TaskRun] = {}

    async def record(self, run: TaskRun) -> None:
        if run.run_id in self._runs:
            # Update path: replace the stored object but leave its
            # position in the eviction order untouched.
            self._runs[run.run_id] = run
            return

        # Insert path: enforce the maxlen invariant before appending so
        # the data structure never grows past the configured bound.
        if len(self._order) >= self._maxlen:
            evicted = self._order.popleft()
            self._runs.pop(evicted, None)
        self._order.append(run.run_id)
        self._runs[run.run_id] = run

    async def list_all(self, limit: int = 500) -> list[TaskRun]:
        if limit <= 0:
            return []
        out: list[TaskRun] = []
        for run_id in reversed(self._order):
            out.append(self._runs[run_id])
            if len(out) >= limit:
                break
        return out

    async def list_by_task(self, task_id: str, limit: int = 100) -> list[TaskRun]:
        if limit <= 0:
            return []
        out: list[TaskRun] = []
        for run_id in reversed(self._order):
            run = self._runs[run_id]
            if run.task_id == task_id:
                out.append(run)
                if len(out) >= limit:
                    break
        return out
