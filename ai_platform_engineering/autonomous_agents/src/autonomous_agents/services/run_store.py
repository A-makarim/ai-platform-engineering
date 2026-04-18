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
from datetime import timezone
from typing import Any, Protocol, runtime_checkable

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


# Default collection name. Kept module-level so callers and tests share the
# same value without re-stating it.
DEFAULT_COLLECTION_NAME = "autonomous_runs"


class MongoRunStore:
    """MongoDB-backed :class:`RunStore` (motor / async).

    Schema (one document per run, ``_id`` mirrors ``run_id``)::

        {
            "_id":              <run_id>,
            "run_id":           <run_id>,
            "task_id":          <task id>,
            "task_name":        <human-readable task name>,
            "status":           "running" | "success" | "failed" | ...,
            "started_at":       <BSON datetime, UTC>,
            "finished_at":      <BSON datetime, UTC> | null,
            "response_preview": <str> | null,
            "error":            <str> | null,
            ... any future fields added to TaskRun ...
        }

    Indexes (created by :meth:`ensure_indexes`):
      - ``run_id`` unique — guards against duplicate inserts on retry.
      - ``(task_id ASC, started_at DESC)`` — serves ``list_by_task``
        (filter + sort) without a collection scan.
      - ``started_at DESC`` — serves the global ``list_all`` sort.
        The compound index above can't be used here because Mongo
        only walks a compound index for a sort if the query also
        filters (or equality-matches) on the leading prefix
        (``task_id``); a plain ``find({})`` falls back to a full
        in-memory sort instead.

    The constructor takes an already-built motor client so the caller
    owns its lifecycle (and tests can inject ``AsyncMongoMockClient``
    from ``mongomock_motor``).
    """

    def __init__(
        self,
        client: Any,
        database_name: str,
        collection_name: str = DEFAULT_COLLECTION_NAME,
    ) -> None:
        if not database_name:
            raise ValueError("database_name must be a non-empty string")
        if not collection_name:
            raise ValueError("collection_name must be a non-empty string")
        self._client = client
        self._collection = client[database_name][collection_name]

    async def ensure_indexes(self) -> None:
        """Create indexes if they don't already exist. Idempotent.

        Call once at application startup. Mongo's ``create_index`` is a
        no-op when the index already exists with the same spec.
        """
        await self._collection.create_index("run_id", unique=True)
        await self._collection.create_index([("task_id", 1), ("started_at", -1)])
        # Required for /runs (list_all): sorts by started_at across
        # all tasks. The compound index above leads on task_id, so
        # Mongo will not use it to back an unfiltered sort.
        await self._collection.create_index([("started_at", -1)])

    async def record(self, run: TaskRun) -> None:
        # model_dump() (default mode="python") preserves datetime objects
        # so pymongo encodes them as native BSON datetime — required for
        # the (task_id, started_at desc) index to be useful.
        doc = run.model_dump()
        # Pin _id to run_id so upserts replace in place rather than
        # creating a new document with a server-generated ObjectId on
        # each call. Without this, a RUNNING -> SUCCESS update would
        # leave two rows behind.
        doc["_id"] = run.run_id
        await self._collection.replace_one({"_id": run.run_id}, doc, upsert=True)

    async def list_all(self, limit: int = 500) -> list[TaskRun]:
        if limit <= 0:
            return []
        cursor = self._collection.find({}, sort=[("started_at", -1)]).limit(limit)
        return [self._doc_to_run(doc) async for doc in cursor]

    async def list_by_task(self, task_id: str, limit: int = 100) -> list[TaskRun]:
        if limit <= 0:
            return []
        cursor = self._collection.find(
            {"task_id": task_id},
            sort=[("started_at", -1)],
        ).limit(limit)
        return [self._doc_to_run(doc) async for doc in cursor]

    @staticmethod
    def _doc_to_run(doc: dict[str, Any]) -> TaskRun:
        # Mongo always returns _id; TaskRun has no such field so strip it
        # before validating to avoid Pydantic raising on extras.
        doc.pop("_id", None)
        return TaskRun.model_validate(doc)


def create_run_store(
    mongodb_uri: str | None = None,
    mongodb_database: str | None = None,
    mongodb_collection: str = DEFAULT_COLLECTION_NAME,
    in_memory_maxlen: int = 500,
) -> RunStore:
    """Build the :class:`RunStore` appropriate for the current configuration.

    Returns a :class:`MongoRunStore` when **both** ``mongodb_uri`` and
    ``mongodb_database`` are provided; otherwise a fresh
    :class:`InMemoryRunStore` bounded by ``in_memory_maxlen``.

    Either Mongo setting on its own is treated as missing — partially
    configured persistence is almost always a misconfiguration, and
    silently falling back to in-memory rather than crashing on startup
    would lose data without the operator noticing.

    The motor client is constructed lazily by the time :meth:`record`
    or any read is awaited; this function does no network I/O.
    """
    if mongodb_uri and mongodb_database:
        # Local import keeps motor optional at import time and lets the
        # in-memory branch run in environments where motor isn't even
        # installed (e.g. minimal test rigs).
        from motor.motor_asyncio import AsyncIOMotorClient

        # ``tz_aware=True`` makes pymongo/motor return UTC-aware
        # ``datetime`` objects when reading BSON dates. Without it,
        # the driver yields naive datetimes and ``TaskRun`` ends up
        # with a mix: ``started_at``/``finished_at`` are tz-aware on
        # the write path (we set ``datetime.now(timezone.utc)``) but
        # tz-naive on the read path (after Mongo round-trip). That
        # breaks downstream comparisons (``a < b`` raises TypeError
        # across naive/aware) and serialises inconsistently in the
        # API response. ``tzinfo=timezone.utc`` pins the returned
        # tzinfo so we never accidentally interpret stored timestamps
        # in local time.
        client = AsyncIOMotorClient(
            mongodb_uri,
            tz_aware=True,
            tzinfo=timezone.utc,
        )
        return MongoRunStore(client, mongodb_database, mongodb_collection)
    return InMemoryRunStore(maxlen=in_memory_maxlen)
