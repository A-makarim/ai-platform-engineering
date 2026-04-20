# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :class:`autonomous_agents.services.mongo.MongoService`.

This is the post-consolidation replacement for the old
``test_mongo_run_store.py`` / ``test_mongo_task_store.py`` /
``test_chat_history.py`` trio. Same assertions -- same mongomock
backend -- just retargeted at the single ``MongoService`` that now
owns every Mongo operation.

Why mongomock_motor? It implements the ``AsyncIOMotorClient`` surface
(including ``_id`` uniqueness, ``replace_one`` matched_count, and
``DuplicateKeyError`` semantics) so we exercise the real code path
without spinning up a Mongo container.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

from autonomous_agents.config import Settings
from autonomous_agents.models import (
    CronTrigger,
    IntervalTrigger,
    TaskDefinition,
    TaskRun,
    TaskStatus,
    WebhookTrigger,
)
from autonomous_agents.services.chat_history import _conversation_id_for_task
from autonomous_agents.services.mongo import (
    MongoChatHistoryPublisherAdapter,
    MongoRunStoreAdapter,
    MongoService,
    MongoTaskStoreAdapter,
    RunStore,
    TaskAlreadyExistsError,
    TaskNotFoundError,
    TaskStore,
)

# Regex mirrored from ``ui/src/lib/api-middleware.ts``::validateUUID so
# a UI-side change forces a deliberate update here too.
_UI_UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _spaced(index: int) -> datetime:
    """``_BASE_TIME + index seconds`` -- above BSON's 1ms precision."""
    return _BASE_TIME + timedelta(seconds=index)


def _settings(**overrides) -> Settings:
    """Build a fresh Settings instance with sensible test defaults.

    Pydantic's ``BaseSettings`` picks up env vars on instantiation, so
    passing explicit overrides rather than relying on the lru-cached
    global ``get_settings()`` keeps tests hermetic.
    """
    base = {
        "mongodb_database": "test_autonomous",
        "mongodb_collection": "autonomous_runs",
        "mongodb_tasks_collection": "autonomous_tasks",
        "chat_history_database": None,
        "chat_history_conversations_collection": "conversations",
        "chat_history_messages_collection": "messages",
        "chat_history_owner_email": "autonomous@system",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def service() -> MongoService:
    """Fresh MongoService backed by an in-memory mock client per test."""
    svc = MongoService(settings=_settings())
    svc.connect_with_client(AsyncMongoMockClient())
    return svc


# ============================================================================
# Lifecycle
# ============================================================================


def test_is_connected_false_before_connect():
    svc = MongoService(settings=_settings())
    assert svc.is_connected is False


def test_is_connected_true_after_connect_with_client():
    svc = MongoService(settings=_settings())
    svc.connect_with_client(AsyncMongoMockClient())
    assert svc.is_connected is True


def test_disconnect_is_idempotent():
    svc = MongoService(settings=_settings())
    svc.connect_with_client(AsyncMongoMockClient())
    svc.disconnect()
    # Calling disconnect again must not raise -- lifespan shutdown may
    # run on a service that was never connected (e.g. failed startup).
    svc.disconnect()
    assert svc.is_connected is False


def test_collection_accessors_raise_when_not_connected():
    """Missing 'await connect()' must surface as a clear error rather
    than an AttributeError deep inside pymongo. Regression guard for
    the refactor: previously each store owned its own client so a
    use-before-connect would raise differently."""
    svc = MongoService(settings=_settings())
    with pytest.raises(RuntimeError, match="not connected"):
        svc._tasks()
    with pytest.raises(RuntimeError, match="not connected"):
        svc._runs()
    with pytest.raises(RuntimeError, match="not connected"):
        svc._conversations()


async def test_connect_refuses_without_mongo_settings():
    """No URI or DB -> connect() returns False, never raises."""
    svc = MongoService(settings=_settings(mongodb_uri=None, mongodb_database=None))
    assert await svc.connect() is False
    assert svc.is_connected is False


# ============================================================================
# Task CRUD
# ============================================================================


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


async def test_create_and_get_round_trip(service: MongoService):
    task = _task("t1")
    created = await service.create_task(task)
    assert created == task

    fetched = await service.get_task("t1")
    assert fetched == task


async def test_get_returns_none_for_missing_task(service: MongoService):
    assert await service.get_task("ghost") is None


async def test_create_translates_duplicate_key_to_typed_error(service: MongoService):
    """The whole point of the typed exception is so the API layer can
    map to 409 without string-matching. Exercise the translation
    against mongomock's real DuplicateKeyError path."""
    await service.create_task(_task("t1"))
    with pytest.raises(TaskAlreadyExistsError) as exc:
        await service.create_task(_task("t1"))
    assert exc.value.task_id == "t1"


async def test_list_tasks_sorted_by_id(service: MongoService):
    for tid in ("zeta", "alpha", "mu"):
        await service.create_task(_task(tid))
    listed = await service.list_tasks()
    assert [t.id for t in listed] == ["alpha", "mu", "zeta"]


async def test_update_replaces_in_place(service: MongoService):
    await service.create_task(_task("t1"))
    new_version = TaskDefinition(
        id="t1",
        name="Renamed",
        agent="argocd",
        prompt="updated",
        trigger=IntervalTrigger(minutes=5),
        enabled=False,
    )
    returned = await service.update_task("t1", new_version)
    assert returned == new_version

    fetched = await service.get_task("t1")
    assert fetched is not None
    assert fetched.name == "Renamed"
    assert fetched.agent == "argocd"
    assert fetched.enabled is False


async def test_update_rejects_id_mismatch(service: MongoService):
    await service.create_task(_task("t1"))
    with pytest.raises(ValueError, match="does not match"):
        await service.update_task("t1", _task("t2"))


async def test_update_raises_when_target_missing(service: MongoService):
    with pytest.raises(TaskNotFoundError) as exc:
        await service.update_task("ghost", _task("ghost"))
    assert exc.value.task_id == "ghost"
    # And no document was upserted as a side-effect.
    assert await service.get_task("ghost") is None


async def test_delete_removes_document(service: MongoService):
    await service.create_task(_task("t1"))
    await service.create_task(_task("t2"))
    await service.delete_task("t1")

    assert await service.get_task("t1") is None
    assert await service.get_task("t2") is not None


async def test_delete_raises_when_target_missing(service: MongoService):
    with pytest.raises(TaskNotFoundError) as exc:
        await service.delete_task("ghost")
    assert exc.value.task_id == "ghost"


async def test_round_trip_preserves_all_trigger_types(service: MongoService):
    """Discriminated trigger unions are easy to break via mode="python"
    dumps -- exercise every variant to lock in the mode="json" path."""
    cron = _task("cron-1", trigger=CronTrigger(schedule="*/5 * * * *"))
    interval = _task("interval-1", trigger=IntervalTrigger(seconds=30))
    webhook = _task("webhook-1", trigger=WebhookTrigger(secret="sssh"))
    for t in (cron, interval, webhook):
        await service.create_task(t)

    assert (await service.get_task("cron-1")).trigger == cron.trigger
    assert (await service.get_task("interval-1")).trigger == interval.trigger
    assert (await service.get_task("webhook-1")).trigger == webhook.trigger


# ============================================================================
# Run history
# ============================================================================


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


async def test_record_run_upserts_in_place(service: MongoService):
    """RUNNING -> SUCCESS must replace the same row, not spawn two."""
    await service.record_run(_make_run("r1", status=TaskStatus.RUNNING))
    updated = _make_run("r1", status=TaskStatus.SUCCESS)
    updated.response_preview = "ok"
    await service.record_run(updated)

    runs = await service.list_runs()
    assert len(runs) == 1
    assert runs[0].status == TaskStatus.SUCCESS
    assert runs[0].response_preview == "ok"


async def test_list_runs_newest_first(service: MongoService):
    for i in range(3):
        await service.record_run(_make_run(f"r{i}", started_at=_spaced(i)))
    runs = await service.list_runs()
    assert [r.run_id for r in runs] == ["r2", "r1", "r0"]


async def test_list_runs_by_task_filters_and_orders(service: MongoService):
    await service.record_run(_make_run("a1", task_id="alpha", started_at=_spaced(0)))
    await service.record_run(_make_run("b1", task_id="beta", started_at=_spaced(1)))
    await service.record_run(_make_run("a2", task_id="alpha", started_at=_spaced(2)))

    alphas = await service.list_runs_by_task("alpha")
    assert [r.run_id for r in alphas] == ["a2", "a1"]

    betas = await service.list_runs_by_task("beta")
    assert [r.run_id for r in betas] == ["b1"]


async def test_list_runs_by_task_empty_for_unknown_task(service: MongoService):
    await service.record_run(_make_run("r1", task_id="alpha"))
    assert await service.list_runs_by_task("does-not-exist") == []


async def test_list_runs_respects_limit(service: MongoService):
    for i in range(5):
        await service.record_run(_make_run(f"r{i}", started_at=_spaced(i)))
    assert len(await service.list_runs(limit=3)) == 3


async def test_zero_or_negative_limit_returns_empty_list(service: MongoService):
    await service.record_run(_make_run("r1"))
    assert await service.list_runs(limit=0) == []
    assert await service.list_runs(limit=-1) == []
    assert await service.list_runs_by_task("t1", limit=0) == []


# ============================================================================
# Chat history
# ============================================================================


async def test_publish_run_writes_one_conversation_two_messages(
    service: MongoService,
):
    run = TaskRun(
        run_id="run-001",
        task_id="weekly-prs",
        task_name="Weekly PR Review",
        status=TaskStatus.SUCCESS,
        started_at=_spaced(0),
        finished_at=_spaced(1),
        response_preview="here",
    )
    await service.publish_run(
        run,
        prompt="list open prs",
        response="here they are",
        error=None,
        agent="github",
    )
    convs = [doc async for doc in service._conversations().find({})]
    msgs = [doc async for doc in service._messages().find({})]
    assert len(convs) == 1
    assert len(msgs) == 2


async def test_conversation_id_matches_ui_uuid_shape(service: MongoService):
    """Deep-link /api/chat/conversations/[id] runs validateUUID on the
    path segment. If our derived id doesn't match the canonical
    8-4-4-4-12 hex shape, every deep-link 400s before hitting Mongo."""
    cid = _conversation_id_for_task(str(uuid.uuid4()))
    assert _UI_UUID_REGEX.match(cid), f"derived id {cid!r} fails UI UUID regex"


async def test_conversation_document_carries_required_ui_fields(
    service: MongoService,
):
    run = TaskRun(
        run_id="r1",
        task_id="weekly-prs",
        task_name="Weekly PR Review",
        status=TaskStatus.SUCCESS,
        started_at=_spaced(0),
        finished_at=_spaced(1),
    )
    await service.publish_run(
        run,
        prompt="hello",
        response="world",
        error=None,
        agent="github",
    )
    conv = await service._conversations().find_one({})
    assert _UI_UUID_REGEX.match(conv["_id"])
    assert conv["source"] == "autonomous"
    assert conv["owner_id"] == "autonomous@system"
    assert conv["agent_id"] == "github"
    assert conv["task_id"] == "weekly-prs"
    assert "autonomous" in conv["tags"]
    assert "weekly-prs" in conv["tags"]
    assert conv["is_archived"] is False
    assert conv["is_pinned"] is False


async def test_publish_run_is_idempotent_across_status_transitions(
    service: MongoService,
):
    """RUNNING -> SUCCESS must not duplicate the conversation or
    append new message rows; it overwrites the two existing slots."""
    run = TaskRun(
        run_id="r1",
        task_id="t1",
        task_name="t1",
        status=TaskStatus.RUNNING,
        started_at=_spaced(0),
    )
    await service.publish_run(
        run, prompt="hello", response=None, error=None, agent="github"
    )
    run.status = TaskStatus.SUCCESS
    run.response_preview = "world"
    await service.publish_run(
        run, prompt="hello", response="world", error=None, agent="github"
    )

    convs = [doc async for doc in service._conversations().find({})]
    msgs = [doc async for doc in service._messages().find({})]
    assert len(convs) == 1
    assert len(msgs) == 2

    assistant = await service._messages().find_one({"role": "assistant"})
    assert assistant["content"] == "world"
    assert assistant["metadata"]["is_final"] is True


async def test_failed_run_assistant_message_carries_error_text(
    service: MongoService,
):
    run = TaskRun(
        run_id="r-fail",
        task_id="t1",
        task_name="t1",
        status=TaskStatus.FAILED,
        started_at=_spaced(0),
        error="boom",
    )
    await service.publish_run(
        run, prompt="please work", response=None, error="boom", agent="github"
    )
    assistant = await service._messages().find_one({"role": "assistant"})
    assert "boom" in assistant["content"]
    assert assistant["metadata"]["is_final"] is True


async def test_publish_creation_intent_and_preflight_ack(service: MongoService):
    """Spec #099 Phase 1 lifecycle messages must land on the same
    per-task conversation and carry metadata.kind tags."""
    task = _task("form-task")
    await service.publish_creation_intent(task)
    await service.publish_preflight_ack(
        task,
        {
            "ack_status": "accepted",
            "ack_detail": "Looks good.",
            "dry_run_summary": "Will ping GitHub for recent PRs.",
            "ack_at": "2026-04-20T10:00:00Z",
        },
    )
    # Exactly one conversation, two messages, one of each kind.
    convs = [doc async for doc in service._conversations().find({})]
    msgs = [doc async for doc in service._messages().find({}).sort("created_at", 1)]
    assert len(convs) == 1
    assert convs[0]["_id"] == _conversation_id_for_task("form-task")
    kinds = [m["metadata"]["kind"] for m in msgs]
    assert "creation_intent" in kinds
    assert "preflight_ack" in kinds


# ============================================================================
# Adapters satisfy the Protocols
# ============================================================================


def test_adapters_satisfy_protocols(service: MongoService):
    """Catch signature drift between Protocol and adapter -- callers
    rely on isinstance checks in the scheduler / routes."""
    assert isinstance(MongoTaskStoreAdapter(service), TaskStore)
    assert isinstance(MongoRunStoreAdapter(service), RunStore)
    # ChatHistoryPublisher is a runtime_checkable Protocol too.
    from autonomous_agents.services.chat_history import ChatHistoryPublisher

    assert isinstance(MongoChatHistoryPublisherAdapter(service), ChatHistoryPublisher)


async def test_task_store_adapter_delegates_to_service(service: MongoService):
    adapter = MongoTaskStoreAdapter(service)
    await adapter.create(_task("t1"))
    got = await adapter.get("t1")
    assert got is not None and got.id == "t1"
    assert len(await adapter.list_all()) == 1
    await adapter.delete("t1")
    assert await adapter.get("t1") is None


async def test_run_store_adapter_delegates_to_service(service: MongoService):
    adapter = MongoRunStoreAdapter(service)
    await adapter.record(_make_run("r1", started_at=_spaced(0)))
    await adapter.record(_make_run("r2", started_at=_spaced(1)))
    all_runs = await adapter.list_all()
    assert [r.run_id for r in all_runs] == ["r2", "r1"]


async def test_chat_publisher_adapter_delegates_to_service(service: MongoService):
    adapter = MongoChatHistoryPublisherAdapter(service)
    run = TaskRun(
        run_id="r1",
        task_id="t1",
        task_name="t1",
        status=TaskStatus.SUCCESS,
        started_at=_spaced(0),
    )
    await adapter.publish_run(
        run, prompt="hi", response="hello", error=None, agent="github"
    )
    assert await service._messages().count_documents({}) == 2


# ============================================================================
# Indexes
# ============================================================================


async def test_ensure_indexes_creates_required_run_and_chat_indexes(
    service: MongoService,
):
    await service._ensure_indexes()

    runs_info = await service._runs().index_information()
    run_keys = {tuple(idx["key"]) for idx in runs_info.values()}
    # Filter+sort for list_runs_by_task.
    assert (("task_id", 1), ("started_at", -1)) in run_keys
    # Unfiltered sort for list_runs.
    assert (("started_at", -1),) in run_keys

    conv_info = await service._conversations().index_information()
    conv_keys = {tuple(idx["key"]) for idx in conv_info.values()}
    assert (("source", 1), ("updated_at", -1)) in conv_keys
    assert (("run_id", 1),) in conv_keys

    msg_info = await service._messages().index_information()
    msg_keys = {tuple(idx["key"]) for idx in msg_info.values()}
    assert (("conversation_id", 1), ("message_id", 1)) in msg_keys


async def test_ensure_indexes_is_idempotent(service: MongoService):
    """Startup calls this unconditionally; repeated invocations must
    not raise so a reload / warm restart can't wedge the service."""
    await service._ensure_indexes()
    await service._ensure_indexes()


# ============================================================================
# Two-db-one-client split
# ============================================================================


def test_chat_db_overrides_primary_when_set():
    """CHAT_HISTORY_DATABASE should pin the chat collections at a
    different logical DB while continuing to share the client."""
    svc = MongoService(
        settings=_settings(
            mongodb_database="primary",
            chat_history_database="chat_only",
        )
    )
    client = AsyncMongoMockClient()
    svc.connect_with_client(client)
    assert svc._primary_db.name == "primary"
    assert svc._chat_db.name == "chat_only"
    # Same underlying client -- no second connection pool.
    assert svc._client is client


def test_chat_db_falls_back_to_primary_when_unset():
    svc = MongoService(
        settings=_settings(
            mongodb_database="primary",
            chat_history_database=None,
        )
    )
    svc.connect_with_client(AsyncMongoMockClient())
    assert svc._primary_db.name == "primary"
    assert svc._chat_db.name == "primary"
