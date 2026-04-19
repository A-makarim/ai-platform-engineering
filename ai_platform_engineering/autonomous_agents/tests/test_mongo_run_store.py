# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MongoDBService run persistence.

Uses ``mongomock_motor.AsyncMongoMockClient`` so the test suite needs no
real MongoDB instance. The mock client honours the same async API as
``motor.motor_asyncio.AsyncIOMotorClient``.

Note on timestamps: BSON datetime has millisecond precision. Tight
``record()`` loops in tests can produce identical ``started_at`` values
for which Mongo's sort order is arbitrary. To keep tests deterministic
we pass explicit, well-spaced ``started_at`` values whenever the test
asserts on ordering.
"""

from datetime import datetime, timedelta, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

from autonomous_agents.models import TaskRun, TaskStatus
from autonomous_agents.services.mongo import DEFAULT_COLLECTION_NAME, MongoDBService

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _make_run(
    run_id: str,
    task_id: str = "t1",
    status: TaskStatus = TaskStatus.RUNNING,
    started_at: datetime | None = None,
) -> TaskRun:
    fields: dict = {
        "run_id": run_id,
        "task_id": task_id,
        "task_name": f"task {task_id}",
        "status": status,
    }
    if started_at is not None:
        fields["started_at"] = started_at
    return TaskRun(**fields)


def _spaced(index: int) -> datetime:
    """Return ``_BASE_TIME + index seconds`` — well above BSON's 1ms precision."""
    return _BASE_TIME + timedelta(seconds=index)


@pytest.fixture
def store() -> MongoDBService:
    """Fresh MongoDBService backed by an in-memory mock client per test."""
    client = AsyncMongoMockClient()
    return MongoDBService(client=client, database_name="test_autonomous_agents")


def test_constructor_rejects_empty_database_name():
    client = AsyncMongoMockClient()
    with pytest.raises(ValueError):
        MongoDBService(client=client, database_name="")


async def test_default_collection_name_is_autonomous_runs(store: MongoDBService):
    # Surface the default through the underlying collection so a refactor
    # of the constant doesn't silently change persisted data location.
    assert store.get_runs_collection().name == DEFAULT_COLLECTION_NAME == "autonomous_runs"


async def test_ensure_indexes_is_idempotent(store: MongoDBService):
    await store.ensure_run_indexes()
    await store.ensure_run_indexes()
    info = await store.get_runs_collection().index_information()
    # _id_ is created automatically by mongo; we add 2 more.
    assert "_id_" in info
    keys = {tuple(idx["key"]) for idx in info.values()}
    assert (("task_id", 1), ("started_at", -1)) in keys
    # Required so list_all() can serve a global sort without a
    # collection scan — the compound index above leads on task_id
    # and is unusable for an unfiltered sort.
    assert (("started_at", -1),) in keys


async def test_ensure_indexes_does_not_create_redundant_run_id_index(store: MongoDBService):
    """Regression: ``record()`` pins ``_id = run_id``, so Mongo's
    automatic ``_id_`` index already enforces uniqueness on run_id.
    Adding a second unique ``run_id`` index would just duplicate
    that guarantee at the cost of an extra B-tree on every write."""
    await store.ensure_run_indexes()
    info = await store.get_runs_collection().index_information()
    keys = {tuple(idx["key"]) for idx in info.values()}
    assert (("run_id", 1),) not in keys


async def test_id_field_enforces_run_id_uniqueness(store: MongoDBService):
    """Belt-and-braces: prove the _id-based uniqueness still holds
    after the dedicated run_id index was removed. Two ``record()``
    calls with the same run_id must collapse into a single document
    via upsert, never raise a duplicate-key error."""
    await store.ensure_run_indexes()
    await store.record_run(_make_run("dup", status=TaskStatus.RUNNING))
    await store.record_run(_make_run("dup", status=TaskStatus.SUCCESS))

    runs = await store.list_runs()
    assert len(runs) == 1
    assert runs[0].status == TaskStatus.SUCCESS


async def test_record_and_list_all_returns_newest_first(store: MongoDBService):
    await store.record_run(_make_run("r1", started_at=_spaced(0)))
    await store.record_run(_make_run("r2", started_at=_spaced(1)))
    await store.record_run(_make_run("r3", started_at=_spaced(2)))

    runs = await store.list_runs()
    assert [r.run_id for r in runs] == ["r3", "r2", "r1"]


async def test_record_upserts_in_place_no_duplicate_documents(store: MongoDBService):
    await store.record_run(_make_run("r1", status=TaskStatus.RUNNING))

    updated = _make_run("r1", status=TaskStatus.SUCCESS)
    updated.response_preview = "ok"
    await store.record_run(updated)

    runs = await store.list_runs()
    assert len(runs) == 1
    assert runs[0].status == TaskStatus.SUCCESS
    assert runs[0].response_preview == "ok"


async def test_list_by_task_filters_and_orders_newest_first(store: MongoDBService):
    await store.record_run(_make_run("a1", task_id="alpha", started_at=_spaced(0)))
    await store.record_run(_make_run("b1", task_id="beta", started_at=_spaced(1)))
    await store.record_run(_make_run("a2", task_id="alpha", started_at=_spaced(2)))
    await store.record_run(_make_run("a3", task_id="alpha", started_at=_spaced(3)))

    alphas = await store.list_runs_by_task("alpha")
    assert [r.run_id for r in alphas] == ["a3", "a2", "a1"]

    betas = await store.list_runs_by_task("beta")
    assert [r.run_id for r in betas] == ["b1"]


async def test_list_by_task_returns_empty_for_unknown_task(store: MongoDBService):
    await store.record_run(_make_run("r1", task_id="alpha"))
    assert await store.list_runs_by_task("does-not-exist") == []


async def test_list_by_task_respects_limit(store: MongoDBService):
    for i in range(5):
        await store.record_run(_make_run(f"a{i}", task_id="alpha", started_at=_spaced(i)))

    limited = await store.list_runs_by_task("alpha", limit=2)
    assert [r.run_id for r in limited] == ["a4", "a3"]


async def test_list_all_respects_limit(store: MongoDBService):
    for i in range(5):
        await store.record_run(_make_run(f"r{i}", started_at=_spaced(i)))

    limited = await store.list_runs(limit=3)
    assert [r.run_id for r in limited] == ["r4", "r3", "r2"]


async def test_zero_or_negative_limit_returns_empty_list(store: MongoDBService):
    await store.record_run(_make_run("r1"))
    assert await store.list_runs(limit=0) == []
    assert await store.list_runs(limit=-1) == []
    assert await store.list_runs_by_task("t1", limit=0) == []


async def test_isolation_between_collections():
    """Two stores pointing at different collections must not see each other's data."""
    client = AsyncMongoMockClient()
    store_a = MongoDBService(client=client, database_name="db", run_collection_name="runs_a")
    store_b = MongoDBService(client=client, database_name="db", run_collection_name="runs_b")

    await store_a.record_run(_make_run("a1"))
    await store_b.record_run(_make_run("b1"))

    assert [r.run_id for r in await store_a.list_runs()] == ["a1"]
    assert [r.run_id for r in await store_b.list_runs()] == ["b1"]
