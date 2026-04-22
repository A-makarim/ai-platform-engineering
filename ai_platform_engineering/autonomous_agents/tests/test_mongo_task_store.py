# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :class:`MongoTaskStore`.

Uses ``mongomock_motor.AsyncMongoMockClient`` so we exercise the real
motor API surface without needing a running MongoDB. The Mongo-specific
behaviour we care about beyond the in-memory contract:

* ``_id`` enforces task-id uniqueness via the automatic index.
* ``DuplicateKeyError`` is translated to :class:`TaskAlreadyExistsError`
  rather than leaking the pymongo type to the API layer.
* ``replace_one`` / ``delete_one`` correctly translate "no documents
  matched" to :class:`TaskNotFoundError`.
* Round-tripping every trigger variant survives the
  ``model_dump(mode='json')`` -> Mongo -> ``model_validate`` cycle.
"""

import pytest
from mongomock_motor import AsyncMongoMockClient

from autonomous_agents.models import (
    CronTrigger,
    IntervalTrigger,
    TaskDefinition,
    WebhookTrigger,
)
from autonomous_agents.services.task_store import (
    DEFAULT_TASKS_COLLECTION_NAME,
    MongoTaskStore,
    TaskAlreadyExistsError,
    TaskNotFoundError,
    TaskStore,
)


def _task(
    task_id: str = "t1",
    *,
    trigger=None,
    enabled: bool = True,
) -> TaskDefinition:
    return TaskDefinition(
        id=task_id,
        name=f"Task {task_id}",
        agent="github",
        prompt="hello",
        trigger=trigger or CronTrigger(schedule="0 9 * * *"),
        enabled=enabled,
    )


@pytest.fixture
def store() -> MongoTaskStore:
    """Fresh in-memory mongomock client per test for full isolation."""
    return MongoTaskStore(AsyncMongoMockClient(), database_name="test_db")


def test_mongo_task_store_implements_protocol(store):
    """Catch accidental signature drift between Protocol and impl —
    mirrors the same guard in test_task_store.py."""
    assert isinstance(store, TaskStore)


def test_constructor_rejects_empty_database_name():
    with pytest.raises(ValueError, match="database_name"):
        MongoTaskStore(AsyncMongoMockClient(), database_name="")


def test_constructor_rejects_empty_collection_name():
    with pytest.raises(ValueError, match="collection_name"):
        MongoTaskStore(
            AsyncMongoMockClient(),
            database_name="db",
            collection_name="",
        )


def test_default_collection_name_is_set():
    """Lock in the collection name — operators rely on it for
    backups / migrations / ad-hoc Mongo queries."""
    assert DEFAULT_TASKS_COLLECTION_NAME == "autonomous_tasks"


async def test_ensure_indexes_is_idempotent(store):
    """``ensure_indexes()`` is currently a no-op but the lifespan code
    calls it unconditionally. Verify it doesn't raise so future index
    additions can't accidentally break startup."""
    await store.ensure_indexes()
    await store.ensure_indexes()


async def test_create_persists_and_get_returns_full_task(store):
    task = _task("t1")
    created = await store.create(task)

    assert created == task
    fetched = await store.get("t1")
    assert fetched == task


async def test_get_returns_none_for_missing_task(store):
    """find_one returns None on miss; we must surface that as None,
    not raise — callers (UI 404, scheduler skip) need a cheap probe."""
    assert await store.get("ghost") is None


async def test_create_translates_duplicate_key_to_typed_error(store):
    """The whole point of the typed exception is so the API layer
    doesn't ``except Exception`` and string-match — exercise the
    real translation against the mongomock duplicate key path."""
    await store.create(_task("t1"))

    with pytest.raises(TaskAlreadyExistsError) as exc:
        await store.create(_task("t1"))

    assert exc.value.task_id == "t1"


async def test_id_uniqueness_is_enforced_by_underlying_id_index(store):
    """Belt-and-braces: even if some path bypassed our ``create()``
    duplicate-check, the ``_id`` field would still reject a second
    insert. This guards against a future refactor that switches to
    ``insert_many`` or upsert by mistake."""
    await store.create(_task("t1"))

    raw_collection = store._collection  # noqa: SLF001 — internals on purpose

    with pytest.raises(Exception) as exc:
        await raw_collection.insert_one({"_id": "t1", "id": "t1", "name": "dup"})

    assert exc.value.__class__.__name__ == "DuplicateKeyError"


async def test_list_all_returns_tasks_sorted_by_id(store):
    """Stable sort = stable UI list. Insertion order is meaningless on
    Mongo (no insertion-order guarantee on a B-tree of ObjectIds), so
    we sort by ``_id`` ascending and tests must reflect that."""
    for tid in ("zeta", "alpha", "mu"):
        await store.create(_task(tid))

    listed = await store.list_all()

    assert [t.id for t in listed] == ["alpha", "mu", "zeta"]


async def test_update_replaces_in_place(store):
    await store.create(_task("t1"))
    new_version = TaskDefinition(
        id="t1",
        name="Renamed",
        agent="argocd",
        prompt="updated",
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


async def test_update_rejects_id_mismatch(store):
    """Same contract as InMemoryTaskStore — path id wins, body must
    agree. Otherwise ``replace_one({"_id": "t1"}, {..., "_id": "t2"})``
    would either silently rename or fail with a Mongo-internal error."""
    await store.create(_task("t1"))

    with pytest.raises(ValueError, match="does not match"):
        await store.update("t1", _task("t2"))


async def test_update_raises_when_target_missing(store):
    """``replace_one`` with ``upsert=False`` returns
    ``matched_count=0`` rather than raising — verify we translate it."""
    with pytest.raises(TaskNotFoundError) as exc:
        await store.update("ghost", _task("ghost"))

    assert exc.value.task_id == "ghost"
    # And no document was upserted as a side-effect.
    assert await store.get("ghost") is None


async def test_delete_removes_document(store):
    await store.create(_task("t1"))
    await store.create(_task("t2"))

    await store.delete("t1")

    assert await store.get("t1") is None
    assert (await store.get("t2")) is not None


async def test_delete_raises_when_target_missing(store):
    """``delete_one`` returns ``deleted_count=0`` on miss — we must
    raise so a stale UI delete surfaces as a clear 404 instead of a
    silent success."""
    with pytest.raises(TaskNotFoundError) as exc:
        await store.delete("ghost")

    assert exc.value.task_id == "ghost"


async def test_round_trip_preserves_all_trigger_types(store):
    """Mongo round-trip must survive ``model_dump(mode='json')`` for
    every TriggerType — discriminated unions in particular are easy to
    break by accidentally serialising the enum as the python repr."""
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


async def test_round_trip_preserves_optional_overrides(store):
    """``timeout_seconds`` / ``max_retries`` are floats/ints with
    constraints — verify they survive Mongo serialisation including
    ``None`` defaults."""
    overridden = TaskDefinition(
        id="t-override",
        name="With overrides",
        agent="github",
        prompt="hi",
        trigger=CronTrigger(schedule="* * * * *"),
        timeout_seconds=42.5,
        max_retries=0,
    )
    await store.create(overridden)

    fetched = await store.get("t-override")
    assert fetched is not None
    assert fetched.timeout_seconds == 42.5
    assert fetched.max_retries == 0
