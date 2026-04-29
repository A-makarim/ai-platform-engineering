# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""IMP-20 — duplicate-trigger detection.

Two layers of test:

1. Helper-level (``services/trigger_dedup.py``) — pure-function
   contract for key derivation across the four trigger sources.
   Cheap, no Mongo, runs first.

2. Integration (``services/mongo.MongoService.try_acquire_trigger``)
   via mongomock — exercises the unique-index race-loss path,
   confirms the same row is returned to the loser, and walks the
   webhook + manual route end-to-end via FastAPI's TestClient with
   the dedup store injected.

The scheduler-side ``_execute_scheduled`` wrapper is exercised
directly with a fake trigger store (no APScheduler in the loop) so
we can assert the dedup-then-execute ordering without a paused
scheduler.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from autonomous_agents.config import Settings, get_settings
from autonomous_agents.models import (
    CronTrigger,
    TaskDefinition,
    TaskRun,
    TaskStatus,
    TriggerInstance,
    TriggerSource,
    WebhookTrigger,
)
from autonomous_agents.routes import webhooks as webhooks_route
from autonomous_agents.routes.webhooks import (
    register_webhook_task as _register,
)
from autonomous_agents.routes.webhooks import (
    router as webhooks_router,
)
from autonomous_agents.services.mongo import MongoService
from autonomous_agents.services.trigger_dedup import (
    derive_manual_dedupe_key,
    derive_scheduled_dedupe_key,
    derive_webhook_dedupe_key,
)


# ============================================================================
# Helper-level tests (pure functions; no Mongo)
# ============================================================================


def _settings(**overrides: Any) -> Settings:
    base = {
        "mongodb_database": "test_autonomous",
        "trigger_dedup_enabled": True,
        "trigger_dedup_ttl_seconds": 7 * 24 * 3600,
        "webhook_delivery_id_headers": [
            "X-GitHub-Delivery",
            "X-Hook-Delivery-Id",
            "X-Webhook-Delivery",
        ],
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Each test gets its own Settings instance (no lru_cache leakage)."""
    yield
    get_settings.cache_clear()


# ---- webhook key derivation -------------------------------------------------


def test_webhook_key_uses_first_present_header_in_order(monkeypatch):
    s = _settings()
    monkeypatch.setattr("autonomous_agents.services.trigger_dedup.get_settings", lambda: s)

    headers = {"X-Hook-Delivery-Id": "second", "X-Webhook-Delivery": "third"}
    key, delivery_id = derive_webhook_dedupe_key("t1", headers, b"{}")
    # The list order is [GitHub, Hook, Webhook] — GitHub absent, so
    # X-Hook-Delivery-Id wins over X-Webhook-Delivery.
    assert key == "webhook:t1:second"
    assert delivery_id == "second"


def test_webhook_key_is_case_insensitive(monkeypatch):
    s = _settings()
    monkeypatch.setattr("autonomous_agents.services.trigger_dedup.get_settings", lambda: s)

    # Real upstream proxies frequently down-case header names. The
    # helper's lookup must survive that — otherwise dedup silently
    # falls through to the body-hash branch and double-fires.
    headers = {"x-github-delivery": "abc-123"}
    key, delivery_id = derive_webhook_dedupe_key("t1", headers, b"{}")
    assert key == "webhook:t1:abc-123"
    assert delivery_id == "abc-123"


def test_webhook_key_strips_header_whitespace(monkeypatch):
    s = _settings()
    monkeypatch.setattr("autonomous_agents.services.trigger_dedup.get_settings", lambda: s)

    # An upstream that injects ``"  abc-123  "`` would otherwise produce
    # a key that mismatches a clean retry. Same value -> same key.
    headers = {"X-GitHub-Delivery": "  abc-123  "}
    key, _ = derive_webhook_dedupe_key("t1", headers, b"{}")
    assert key == "webhook:t1:abc-123"


def test_webhook_key_falls_back_to_body_hash(monkeypatch):
    s = _settings()
    monkeypatch.setattr("autonomous_agents.services.trigger_dedup.get_settings", lambda: s)

    body = b'{"event":"push","sha":"abc"}'
    expected = hashlib.sha256(body).hexdigest()[:16]

    key, delivery_id = derive_webhook_dedupe_key("t1", {}, body)
    assert key == f"webhook:t1:body-{expected}"
    # delivery_id is None on the fallback path so the audit row
    # records that no upstream id was supplied (cf. an empty string,
    # which would falsely imply the sender sent ``""``).
    assert delivery_id is None


def test_webhook_key_body_hash_differs_per_body(monkeypatch):
    s = _settings()
    monkeypatch.setattr("autonomous_agents.services.trigger_dedup.get_settings", lambda: s)

    a, _ = derive_webhook_dedupe_key("t1", {}, b'{"x":1}')
    b, _ = derive_webhook_dedupe_key("t1", {}, b'{"x":2}')
    assert a != b


# ---- scheduled key derivation -----------------------------------------------


def test_scheduled_key_format_is_source_task_iso():
    fire_time = datetime(2026, 4, 29, 9, 0, 0, tzinfo=timezone.utc)
    key = derive_scheduled_dedupe_key("nightly-scan", TriggerSource.CRON, fire_time)
    assert key == "cron:nightly-scan:2026-04-29T09:00:00+00:00"


def test_scheduled_key_strips_microseconds():
    """Two replicas fire ~ms apart on the same boundary -- rounding to
    second precision MUST collapse them to one key, otherwise the
    Mongo unique index never sees the conflict."""
    fire_a = datetime(2026, 4, 29, 9, 0, 0, 123_000, tzinfo=timezone.utc)
    fire_b = datetime(2026, 4, 29, 9, 0, 0, 987_654, tzinfo=timezone.utc)
    key_a = derive_scheduled_dedupe_key("t1", TriggerSource.INTERVAL, fire_a)
    key_b = derive_scheduled_dedupe_key("t1", TriggerSource.INTERVAL, fire_b)
    assert key_a == key_b


def test_scheduled_key_normalises_naive_datetime_to_utc():
    naive = datetime(2026, 4, 29, 9, 0, 0)
    key = derive_scheduled_dedupe_key("t1", TriggerSource.CRON, naive)
    assert key.endswith("+00:00")


def test_scheduled_key_rejects_non_scheduled_source():
    with pytest.raises(ValueError, match="non-scheduled source"):
        derive_scheduled_dedupe_key(
            "t1", TriggerSource.WEBHOOK, datetime.now(timezone.utc)
        )


def test_scheduled_key_distinguishes_cron_from_interval():
    """A cron and an interval task that happen to fire on the same
    second must NOT dedupe each other -- the source prefix keeps the
    namespaces disjoint."""
    fire = datetime(2026, 4, 29, 9, 0, 0, tzinfo=timezone.utc)
    cron_key = derive_scheduled_dedupe_key("t1", TriggerSource.CRON, fire)
    int_key = derive_scheduled_dedupe_key("t1", TriggerSource.INTERVAL, fire)
    assert cron_key != int_key


# ---- manual key derivation --------------------------------------------------


def test_manual_key_with_idempotency_key_is_deterministic():
    a, used_a = derive_manual_dedupe_key("t1", "client-key-1")
    b, used_b = derive_manual_dedupe_key("t1", "client-key-1")
    assert a == b
    assert used_a == used_b == "client-key-1"


def test_manual_key_without_idempotency_key_is_unique_per_call():
    """No header == no dedup. Two clicks produce two distinct keys
    so the legacy fire-and-forget UX is preserved."""
    a, used_a = derive_manual_dedupe_key("t1", None)
    b, used_b = derive_manual_dedupe_key("t1", None)
    assert a != b
    assert used_a is None and used_b is None


def test_manual_key_treats_empty_string_as_no_key():
    """Defensive: an empty Idempotency-Key header is meaningless and
    must NOT collapse every empty-key request to one fire."""
    a, used_a = derive_manual_dedupe_key("t1", "")
    b, used_b = derive_manual_dedupe_key("t1", "   ")
    assert a != b
    assert used_a is None and used_b is None


# ============================================================================
# MongoService.try_acquire_trigger — race semantics
# ============================================================================


def _service(**setting_overrides: Any) -> MongoService:
    svc = MongoService(settings=_settings(**setting_overrides))
    svc.connect_with_client(AsyncMongoMockClient())
    return svc


def _instance(*, dedupe_key: str, source: TriggerSource = TriggerSource.WEBHOOK) -> TriggerInstance:
    return TriggerInstance(
        trigger_id=str(uuid.uuid4()),
        task_id="t1",
        dedupe_key=dedupe_key,
        source=source,
        received_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_try_acquire_trigger_first_caller_wins():
    svc = _service()
    inst = _instance(dedupe_key="webhook:t1:abc")

    stored, acquired = await svc.try_acquire_trigger(inst)

    assert acquired is True
    assert stored is inst  # original returned unchanged on success


@pytest.mark.asyncio
async def test_try_acquire_trigger_second_caller_loses_and_gets_original():
    """The unique index on dedupe_key is the whole point. The loser
    must see acquired=False AND get the winner's row back so it can
    forward the original run_id to the upstream sender."""
    svc = _service()
    # Mongomock's mock client does not auto-create indexes from
    # connect_with_client, so we ensure them manually here.
    await svc._ensure_indexes()

    first = _instance(dedupe_key="webhook:t1:abc")
    first.run_id = "run-from-first"  # pretend the run already started
    first_stored, first_acquired = await svc.try_acquire_trigger(first)
    assert first_acquired is True

    second = _instance(dedupe_key="webhook:t1:abc")  # same key
    second_stored, second_acquired = await svc.try_acquire_trigger(second)

    assert second_acquired is False
    # The row we hand back is the WINNER's row, not the loser's
    # (so the caller can forward the original run id).
    assert second_stored.trigger_id == first.trigger_id
    assert second_stored.run_id == "run-from-first"


@pytest.mark.asyncio
async def test_try_acquire_trigger_kill_switch_skips_mongo_entirely():
    """When ``trigger_dedup_enabled=False`` the helper must always
    return acquired=True without touching Mongo -- otherwise the
    rollback knob doesn't actually roll back."""
    svc = _service(trigger_dedup_enabled=False)
    # No ensure_indexes call: with the kill switch on, the collection
    # should never be touched at all. If the implementation regresses
    # to insert anyway we'd see a unique-index race below.

    inst1 = _instance(dedupe_key="webhook:t1:same")
    inst2 = _instance(dedupe_key="webhook:t1:same")
    _, ok1 = await svc.try_acquire_trigger(inst1)
    _, ok2 = await svc.try_acquire_trigger(inst2)

    assert ok1 is True and ok2 is True


@pytest.mark.asyncio
async def test_attach_run_to_trigger_writes_back_reference():
    svc = _service()
    await svc._ensure_indexes()

    inst = _instance(dedupe_key="webhook:t1:xyz")
    await svc.try_acquire_trigger(inst)

    await svc.attach_run_to_trigger(inst.trigger_id, "run-42")

    fetched = await svc.get_trigger_instance(inst.trigger_id)
    assert fetched is not None
    assert fetched.run_id == "run-42"


@pytest.mark.asyncio
async def test_attach_run_to_trigger_swallows_missing_target():
    """Best-effort contract: a write to a non-existent trigger must
    not raise. Mirrors ``_record_safely`` in the scheduler."""
    svc = _service()
    # Should be a silent no-op (update_one matches zero docs).
    await svc.attach_run_to_trigger("does-not-exist", "run-1")


# ============================================================================
# In-memory TriggerInstanceStore fake (used by route + scheduler tests)
# ============================================================================


class _RecordingTriggerStore:
    """Tiny in-memory ``TriggerInstanceStore`` for the route tests.

    Mirrors the Mongo unique-index contract: the second insert with
    the same ``dedupe_key`` returns ``acquired=False`` plus the
    original row. No need to spin up mongomock for these tests.
    """

    def __init__(self) -> None:
        self.rows: dict[str, TriggerInstance] = {}

    async def try_acquire_trigger(self, inst):
        existing = self.rows.get(inst.dedupe_key)
        if existing is not None:
            return existing, False
        self.rows[inst.dedupe_key] = inst
        return inst, True

    async def attach_run_to_trigger(self, trigger_id, run_id):
        for row in self.rows.values():
            if row.trigger_id == trigger_id:
                row.run_id = run_id
                return


# ============================================================================
# Webhook route end-to-end (TestClient + injected store)
# ============================================================================


def _make_webhook_task(task_id: str = "wh-1") -> TaskDefinition:
    return TaskDefinition(
        id=task_id,
        name="webhook task",
        agent="dummy-agent",
        prompt="run the thing",
        trigger=WebhookTrigger(),
    )


@pytest.fixture
def webhook_app(monkeypatch):
    """FastAPI app + injected dedup store + stub fire_webhook_task.

    Uses a small in-memory ``_RecordingTriggerStore`` rather than
    mongomock here -- the Mongo unique-index race semantics are
    already covered by the dedicated ``MongoService.try_acquire_trigger``
    tests above. Mixing mongomock's per-loop locks with FastAPI's
    request-thread loops causes intermittent flakes that aren't
    representative of production.
    """
    app = FastAPI()
    app.include_router(webhooks_router, prefix="/api/v1")

    webhooks_route._webhook_tasks.clear()

    settings = _settings()
    monkeypatch.setattr(webhooks_route, "get_settings", lambda: settings)

    store = _RecordingTriggerStore()
    monkeypatch.setattr(webhooks_route, "_resolve_trigger_store", lambda: store)

    fired: list[dict[str, Any]] = []

    async def _fake_fire(
        task: TaskDefinition,
        context: dict[str, Any],
        *,
        trigger_id: str | None = None,
    ) -> TaskRun:
        run = TaskRun(
            run_id=f"run-{len(fired) + 1}",
            task_id=task.id,
            task_name=task.name,
            status=TaskStatus.SUCCESS,
            trigger_id=trigger_id,
        )
        fired.append({"task_id": task.id, "context": context, "trigger_id": trigger_id, "run_id": run.run_id})
        # Mirror the production write-back so subsequent dedup hits
        # see the original run id (matches MongoService.attach_run_to_trigger).
        if trigger_id is not None:
            await store.attach_run_to_trigger(trigger_id, run.run_id)
        return run

    monkeypatch.setattr(webhooks_route, "fire_webhook_task", _fake_fire)

    with TestClient(app) as client:
        client.fired = fired  # type: ignore[attr-defined]
        client.store = store  # type: ignore[attr-defined]
        yield client

    webhooks_route._webhook_tasks.clear()


def test_webhook_with_delivery_header_dedupes_repeats(webhook_app):
    _register(_make_webhook_task())

    headers = {"X-GitHub-Delivery": "delivery-1"}
    body = b'{"event":"push"}'

    first = webhook_app.post("/api/v1/hooks/wh-1", content=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "accepted"
    first_trigger = first.json()["trigger_id"]
    first_run = first.json()["run_id"]
    assert first_trigger and first_run

    second = webhook_app.post("/api/v1/hooks/wh-1", content=body, headers=headers)
    assert second.status_code == 200
    body2 = second.json()
    assert body2["status"] == "duplicate"
    # Same trigger row, same run id forwarded back so the upstream
    # sender can deep-link without re-querying.
    assert body2["trigger_id"] == first_trigger
    assert body2["run_id"] == first_run

    # Critically: only ONE run actually fired despite TWO POSTs.
    assert len(webhook_app.fired) == 1


def test_webhook_without_delivery_header_uses_body_hash(webhook_app):
    """Senders that don't include a delivery id still dedupe on the
    body hash within TTL. Identical bodies twice -> one run."""
    _register(_make_webhook_task())

    body = json.dumps({"event": "merge", "pr": 42}).encode()
    first = webhook_app.post("/api/v1/hooks/wh-1", content=body)
    second = webhook_app.post("/api/v1/hooks/wh-1", content=body)

    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["status"] == "duplicate"
    assert len(webhook_app.fired) == 1


def test_webhook_different_bodies_each_fire(webhook_app):
    """Distinct bodies must NOT dedupe -- they're distinct events."""
    _register(_make_webhook_task())

    a = webhook_app.post("/api/v1/hooks/wh-1", content=b'{"x":1}')
    b = webhook_app.post("/api/v1/hooks/wh-1", content=b'{"x":2}')

    assert a.json()["status"] == "accepted"
    assert b.json()["status"] == "accepted"
    assert len(webhook_app.fired) == 2


def test_webhook_dedup_failure_falls_through_to_fire(webhook_app, monkeypatch):
    """A flaky Mongo on the dedup write must NOT reject a valid
    webhook. We'd rather risk a duplicate run (visible in history)
    than drop a legitimate event silently."""
    _register(_make_webhook_task())

    async def _boom(_inst):
        raise RuntimeError("simulated mongo outage")

    monkeypatch.setattr(webhook_app.store, "try_acquire_trigger", _boom)

    resp = webhook_app.post("/api/v1/hooks/wh-1", content=b"{}")

    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"
    # trigger_id is None on the fallback path -- no row was written.
    assert resp.json()["trigger_id"] is None
    assert len(webhook_app.fired) == 1


# ============================================================================
# Scheduler wrapper — _execute_scheduled
# ============================================================================


@pytest.fixture(autouse=True)
def _reset_scheduler_modules():
    import autonomous_agents.scheduler as scheduler_mod

    saved = (
        scheduler_mod._run_store,
        scheduler_mod._trigger_store,
        scheduler_mod._task_resolver,
    )
    scheduler_mod._run_store = None
    scheduler_mod._trigger_store = None
    scheduler_mod._task_resolver = None
    yield
    (
        scheduler_mod._run_store,
        scheduler_mod._trigger_store,
        scheduler_mod._task_resolver,
    ) = saved


@pytest.mark.asyncio
async def test_execute_scheduled_dedupes_same_second_fires():
    """Two _execute_scheduled invocations within the same second
    (e.g. multi-replica race) must produce exactly one run."""
    from autonomous_agents.scheduler import (
        _execute_scheduled,
        execute_task,
        set_run_store,
        set_task_resolver,
        set_trigger_store,
    )

    task = TaskDefinition(
        id="cron-1",
        name="Cron 1",
        agent="github",
        prompt="hello",
        trigger=CronTrigger(schedule="* * * * *"),
    )

    store_calls: list[TaskRun] = []

    class _Recording:
        async def record(self, run):
            store_calls.append(run)

        async def list_all(self, limit=500):  # pragma: no cover
            return []

        async def list_by_task(self, task_id, limit=100):  # pragma: no cover
            return []

    set_run_store(_Recording())
    set_trigger_store(_RecordingTriggerStore())

    async def _resolver(task_id):
        return task if task_id == task.id else None

    set_task_resolver(_resolver)

    fixed_now = datetime(2026, 4, 29, 9, 0, 0, 250_000, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    with patch("autonomous_agents.scheduler.datetime", _FixedDatetime), patch(
        "autonomous_agents.scheduler.invoke_agent_streaming",
        new=AsyncMock(return_value=("ok", [])),
    ):
        await _execute_scheduled(task.id, TriggerSource.CRON)
        await _execute_scheduled(task.id, TriggerSource.CRON)

    # One execute_task call -> two record() invocations (RUNNING + SUCCESS),
    # not four. The second fire short-circuits at the dedup gate.
    assert len(store_calls) == 2, store_calls
    # Both record calls describe the SAME run.
    assert store_calls[0].run_id == store_calls[1].run_id
    assert store_calls[1].status == TaskStatus.SUCCESS

    # Reference execute_task to keep the import non-redundant for
    # readers cross-checking the contract.
    assert callable(execute_task)


@pytest.mark.asyncio
async def test_execute_scheduled_skips_when_task_disabled():
    """A task that's been disabled between registration and fire time
    must NOT run -- and must not consume a dedup slot either, so the
    operator can re-enable + manually run later in the same second."""
    from autonomous_agents.scheduler import (
        _execute_scheduled,
        set_run_store,
        set_task_resolver,
        set_trigger_store,
    )

    task = TaskDefinition(
        id="cron-1",
        name="Cron 1",
        agent="github",
        prompt="hello",
        trigger=CronTrigger(schedule="* * * * *"),
        enabled=False,
    )

    record_calls: list[TaskRun] = []

    class _Recording:
        async def record(self, run):
            record_calls.append(run)

        async def list_all(self, limit=500):  # pragma: no cover
            return []

        async def list_by_task(self, task_id, limit=100):  # pragma: no cover
            return []

    store = _RecordingTriggerStore()
    set_run_store(_Recording())
    set_trigger_store(store)

    async def _resolver(task_id):
        return task

    set_task_resolver(_resolver)

    await _execute_scheduled(task.id, TriggerSource.CRON)

    assert record_calls == []
    assert store.rows == {}


@pytest.mark.asyncio
async def test_execute_scheduled_continues_when_dedup_store_raises():
    """Mongo failure on the dedup write must not block a scheduled
    fire -- we'd rather risk a duplicate run than miss a cron tick."""
    from autonomous_agents.scheduler import (
        _execute_scheduled,
        set_run_store,
        set_task_resolver,
        set_trigger_store,
    )

    task = TaskDefinition(
        id="cron-1",
        name="Cron 1",
        agent="github",
        prompt="hello",
        trigger=CronTrigger(schedule="* * * * *"),
    )

    record_calls: list[TaskRun] = []

    class _Recording:
        async def record(self, run):
            record_calls.append(run)

        async def list_all(self, limit=500):  # pragma: no cover
            return []

        async def list_by_task(self, task_id, limit=100):  # pragma: no cover
            return []

    class _BoomStore:
        async def try_acquire_trigger(self, inst):
            raise RuntimeError("simulated outage")

        async def attach_run_to_trigger(self, trigger_id, run_id):  # pragma: no cover
            pass

    set_run_store(_Recording())
    set_trigger_store(_BoomStore())

    async def _resolver(task_id):
        return task

    set_task_resolver(_resolver)

    with patch(
        "autonomous_agents.scheduler.invoke_agent_streaming",
        new=AsyncMock(return_value=("ok", [])),
    ):
        await _execute_scheduled(task.id, TriggerSource.CRON)

    # The run still happens -- two record() calls, one RUNNING + one SUCCESS.
    assert len(record_calls) == 2
    assert record_calls[1].status == TaskStatus.SUCCESS


# ============================================================================
# TaskRun cross-reference
# ============================================================================


@pytest.mark.asyncio
async def test_execute_task_stamps_trigger_id_on_run():
    """The trigger_id passed in MUST land on the persisted TaskRun
    so the audit chain trigger -> run -> conversation is walkable."""
    from autonomous_agents.scheduler import (
        execute_task,
        set_run_store,
        set_trigger_store,
    )

    task = TaskDefinition(
        id="t1",
        name="T1",
        agent="github",
        prompt="hi",
        trigger=CronTrigger(schedule="0 9 * * *"),
    )

    persisted: list[TaskRun] = []

    class _Recording:
        async def record(self, run):
            persisted.append(run)

        async def list_all(self, limit=500):  # pragma: no cover
            return []

        async def list_by_task(self, task_id, limit=100):  # pragma: no cover
            return []

    attach_calls: list[tuple[str, str]] = []

    class _StoreCapture:
        async def try_acquire_trigger(self, inst):  # pragma: no cover -- not used here
            return inst, True

        async def attach_run_to_trigger(self, trigger_id, run_id):
            attach_calls.append((trigger_id, run_id))

    set_run_store(_Recording())
    set_trigger_store(_StoreCapture())

    with patch(
        "autonomous_agents.scheduler.invoke_agent_streaming",
        new=AsyncMock(return_value=("done", [])),
    ):
        run = await execute_task(task, trigger_id="trigger-xyz")

    assert run.trigger_id == "trigger-xyz"
    assert all(r.trigger_id == "trigger-xyz" for r in persisted)
    # attach_run_to_trigger was called exactly once with the matching pair.
    assert attach_calls == [("trigger-xyz", run.run_id)]
