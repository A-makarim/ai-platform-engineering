# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Task definition persistence layer.

Mirrors the :mod:`run_store` Protocol pattern: a small async interface so
the rest of the service can CRUD :class:`TaskDefinition` records without
caring whether they live in memory or in MongoDB.

The default :class:`InMemoryTaskStore` retains the legacy "tasks live in
``config.yaml`` only" behaviour: tasks are loaded into memory at startup
and any UI-driven create/update/delete is lost on restart. This is fine
for development and CI; production should set ``MONGODB_URI`` /
``MONGODB_DATABASE`` to enable :class:`MongoTaskStore`, which persists
edits across restarts and lets a future multi-replica deployment share
the same task catalogue.

Why a separate store from ``RunStore``? Run history is append-mostly
observability data (write-heavy, time-ordered, evictable). Task
definitions are the source of truth for *what* the scheduler should run
(read-heavy, mutated by humans, must never be silently lost). Mixing
them in one collection would conflate two very different durability
contracts.
"""

from typing import Any, Protocol, runtime_checkable

from autonomous_agents.models import TaskDefinition


class TaskAlreadyExistsError(Exception):
    """Raised by :meth:`TaskStore.create` when the ``task_id`` is taken.

    Lifted to its own type so the API layer can map it to a clean HTTP
    409 without resorting to string-matching the message.
    """

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task '{task_id}' already exists")
        self.task_id = task_id


class TaskNotFoundError(Exception):
    """Raised by :meth:`TaskStore.update` / :meth:`delete` for unknown ids.

    Lets the API layer turn missing-task errors into HTTP 404 without
    needing a separate "does it exist?" round-trip first (which would
    race with concurrent deletes anyway).
    """

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task '{task_id}' not found")
        self.task_id = task_id


@runtime_checkable
class TaskStore(Protocol):
    """Async CRUD interface for :class:`TaskDefinition` records.

    Implementations MUST be safe to call concurrently from the FastAPI
    event loop. Mutating methods are atomic per call: a failed
    :meth:`create` / :meth:`update` / :meth:`delete` MUST leave the
    store in its prior state.
    """

    async def list_all(self) -> list[TaskDefinition]:
        """Return every task in stable order (insertion order for
        in-memory; ``_id`` ascending for Mongo). The list is a snapshot;
        callers are free to mutate it."""
        ...

    async def get(self, task_id: str) -> TaskDefinition | None:
        """Return the task with ``task_id`` or ``None`` if not present."""
        ...

    async def create(self, task: TaskDefinition) -> TaskDefinition:
        """Insert ``task``. Raises :class:`TaskAlreadyExistsError` if
        ``task.id`` collides with an existing record."""
        ...

    async def update(self, task_id: str, task: TaskDefinition) -> TaskDefinition:
        """Full-replace the task with ``task_id``.

        ``task.id`` MUST equal ``task_id``; the store does not support
        renaming records (id changes are deletes + creates, which is a
        clearer audit trail). Raises :class:`TaskNotFoundError` if the
        target does not exist.
        """
        ...

    async def delete(self, task_id: str) -> None:
        """Remove the task with ``task_id``. Raises
        :class:`TaskNotFoundError` if it does not exist — a no-op delete
        usually masks a race condition the caller should know about."""
        ...


class InMemoryTaskStore:
    """In-memory :class:`TaskStore` — the default when Mongo is not configured.

    Backed by a plain ``dict`` so insertion order is preserved (CPython
    3.7+ dict guarantees). Suitable for development, CI, and any
    deployment where the YAML seed file is the only source of truth.

    Thread/task safety: assumes a single asyncio event loop driver.
    Each mutating coroutine runs to completion before yielding, so no
    locking is needed.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskDefinition] = {}

    async def list_all(self) -> list[TaskDefinition]:
        # ``list(values())`` snapshots the current state so callers
        # iterating the result cannot trip on concurrent mutations.
        return list(self._tasks.values())

    async def get(self, task_id: str) -> TaskDefinition | None:
        return self._tasks.get(task_id)

    async def create(self, task: TaskDefinition) -> TaskDefinition:
        if task.id in self._tasks:
            raise TaskAlreadyExistsError(task.id)
        self._tasks[task.id] = task
        return task

    async def update(self, task_id: str, task: TaskDefinition) -> TaskDefinition:
        if task.id != task_id:
            # Reject id mismatches loudly. Silently accepting them would
            # let a UI bug rename tasks on PUT, breaking the URL <-> id
            # contract every caller relies on.
            raise ValueError(
                f"path task_id '{task_id}' does not match body id '{task.id}'"
            )
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        self._tasks[task_id] = task
        return task

    async def delete(self, task_id: str) -> None:
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        del self._tasks[task_id]


# Default Mongo collection name for task definitions. Module-level so
# the factory and tests reference the same string.
DEFAULT_TASKS_COLLECTION_NAME = "autonomous_tasks"


class MongoTaskStore:
    """MongoDB-backed :class:`TaskStore` (motor / async).

    Schema (one document per task, ``_id`` mirrors ``task.id``)::

        {
            "_id":             <task.id>,
            "id":              <task.id>,
            "name":            <human-readable name>,
            "description":     <str> | null,
            "agent":           <agent id>,
            "prompt":          <str>,
            "trigger":         { "type": "cron"|"interval"|"webhook", ... },
            "llm_provider":    <str> | null,
            "enabled":         <bool>,
            "metadata":        { ... },
            "timeout_seconds": <float> | null,
            "max_retries":     <int> | null
        }

    Mongo's automatic ``_id_`` index enforces uniqueness on ``task.id``
    because :meth:`create` pins ``_id = task.id`` — so we don't add a
    redundant unique index on ``id``. :meth:`ensure_indexes` is still
    defined (no-op today) so callers don't need to special-case backends.

    The constructor takes an already-built motor client so the caller
    owns its lifecycle (and tests can inject ``AsyncMongoMockClient``).
    """

    def __init__(
        self,
        client: Any,
        database_name: str,
        collection_name: str = DEFAULT_TASKS_COLLECTION_NAME,
    ) -> None:
        if not database_name:
            raise ValueError("database_name must be a non-empty string")
        if not collection_name:
            raise ValueError("collection_name must be a non-empty string")
        self._client = client
        self._collection = client[database_name][collection_name]

    async def ensure_indexes(self) -> None:
        """Create indexes if they don't already exist. Idempotent.

        Currently a no-op: Mongo's automatic ``_id_`` index already
        covers our only access pattern (lookup by task id) and the
        full-collection scan from :meth:`list_all` is bounded by the
        small number of tasks an operator realistically defines.

        Defined anyway so the lifespan code in ``main.py`` can call
        ``ensure_indexes()`` uniformly across stores and we have a hook
        when future access patterns demand a real index.
        """
        return None

    async def list_all(self) -> list[TaskDefinition]:
        cursor = self._collection.find({}, sort=[("_id", 1)])
        return [self._doc_to_task(doc) async for doc in cursor]

    async def get(self, task_id: str) -> TaskDefinition | None:
        doc = await self._collection.find_one({"_id": task_id})
        return self._doc_to_task(doc) if doc else None

    async def create(self, task: TaskDefinition) -> TaskDefinition:
        doc = self._task_to_doc(task)
        try:
            await self._collection.insert_one(doc)
        except Exception as exc:  # noqa: BLE001 — re-raised as a typed error
            # ``DuplicateKeyError`` lives in ``pymongo.errors`` but
            # importing it eagerly would force a hard dep on pymongo
            # for callers that only use the in-memory store. Detect it
            # by the class name instead — this is the same trick motor
            # itself uses internally for cross-version compatibility.
            if exc.__class__.__name__ == "DuplicateKeyError":
                raise TaskAlreadyExistsError(task.id) from exc
            raise
        return task

    async def update(self, task_id: str, task: TaskDefinition) -> TaskDefinition:
        if task.id != task_id:
            raise ValueError(
                f"path task_id '{task_id}' does not match body id '{task.id}'"
            )
        doc = self._task_to_doc(task)
        # ``replace_one`` with ``upsert=False`` returns ``matched_count == 0``
        # when the target is missing rather than raising — translate that
        # to TaskNotFoundError so the API layer's contract is uniform
        # across backends.
        result = await self._collection.replace_one({"_id": task_id}, doc, upsert=False)
        if result.matched_count == 0:
            raise TaskNotFoundError(task_id)
        return task

    async def delete(self, task_id: str) -> None:
        result = await self._collection.delete_one({"_id": task_id})
        if result.deleted_count == 0:
            raise TaskNotFoundError(task_id)

    @staticmethod
    def _task_to_doc(task: TaskDefinition) -> dict[str, Any]:
        # ``mode="json"`` so enum values (e.g. ``TriggerType.CRON``)
        # serialise to their string representation — Mongo can't store
        # Python enums and a future read would raise on validation.
        doc = task.model_dump(mode="json")
        doc["_id"] = task.id
        return doc

    @staticmethod
    def _doc_to_task(doc: dict[str, Any]) -> TaskDefinition:
        # Strip the Mongo-internal _id before validation; TaskDefinition
        # has no such field and Pydantic would reject it as an extra.
        doc.pop("_id", None)
        return TaskDefinition.model_validate(doc)


def create_task_store(
    mongodb_uri: str | None = None,
    mongodb_database: str | None = None,
    mongodb_collection: str = DEFAULT_TASKS_COLLECTION_NAME,
) -> TaskStore:
    """Build the :class:`TaskStore` appropriate for the current configuration.

    Returns a :class:`MongoTaskStore` when **both** ``mongodb_uri`` and
    ``mongodb_database`` are provided; otherwise an :class:`InMemoryTaskStore`.

    Same partial-config policy as :func:`create_run_store`: either Mongo
    setting on its own is treated as missing rather than crashing on
    startup, but the operator is still expected to set both for any
    deployment that needs persistence.
    """
    if mongodb_uri and mongodb_database:
        # Lazy import keeps the motor dependency out of the call graph
        # for the in-memory branch. Same rationale as the run_store
        # factory; see the comment there for the full story.
        from datetime import timezone

        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(
            mongodb_uri,
            tz_aware=True,
            tzinfo=timezone.utc,
        )
        return MongoTaskStore(client, mongodb_database, mongodb_collection)
    return InMemoryTaskStore()
