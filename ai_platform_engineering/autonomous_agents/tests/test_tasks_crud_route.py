# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the /tasks CRUD endpoints.

These exercise the FastAPI router via ``TestClient`` against an
in-file ``TaskStore`` fake and a paused ``AsyncIOScheduler``,
asserting both the HTTP contract (status codes, payload shapes) AND
the runtime side effects (scheduler/webhook registry are kept in
sync). The latter is the whole point of the hot-reload helpers --
without these checks, a regression would only surface when an
operator noticed that a UI edit "looked saved but never fired".

Why a tiny in-file fake instead of mongomock
--------------------------------------------
Production uses :class:`MongoTaskStoreAdapter` (backed by MongoDB).
These router tests only care about "does the handler delegate to its
store correctly?" -- so a minimal Protocol-satisfying fake keeps
failure diagnostics pointing at the router rather than at Mongo
semantics. Tests that cover Mongo-specific behaviour live in
``test_mongo_service.py``.
"""

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autonomous_agents.models import TaskDefinition
from autonomous_agents.routes import tasks as tasks_route
from autonomous_agents.routes import webhooks as webhooks_route
from autonomous_agents.scheduler import get_scheduler
from autonomous_agents.services.mongo import (
    TaskAlreadyExistsError,
    TaskNotFoundError,
)


class _DictTaskStore:
    """Minimal in-file ``TaskStore`` fake -- same rationale as
    ``test_tasks_route._DictTaskStore``. Duplicated intentionally so
    each test module is self-contained and the Protocol surface used
    is visible in a single glance."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskDefinition] = {}

    async def list_all(self) -> list[TaskDefinition]:
        return list(self._tasks.values())

    async def get(self, task_id: str) -> TaskDefinition | None:
        return self._tasks.get(task_id)

    async def create(self, task: TaskDefinition) -> TaskDefinition:
        if task.id in self._tasks:
            raise TaskAlreadyExistsError(task.id)
        self._tasks[task.id] = task
        return task

    async def update(
        self, task_id: str, task: TaskDefinition
    ) -> TaskDefinition:
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        self._tasks[task_id] = task
        return task

    async def delete(self, task_id: str) -> None:
        if task_id not in self._tasks:
            raise TaskNotFoundError(task_id)
        del self._tasks[task_id]


@pytest.fixture
def client():
    """Assemble a minimal FastAPI app with only the /tasks router.

    We deliberately *don't* use the real ``create_app`` lifespan -- it
    connects to MongoDB and loads YAML we don't need here. Instead we
    wire the in-file fake store by hand and substitute the real
    ``AsyncIOScheduler`` (which requires a running event loop) with a
    ``BackgroundScheduler`` started in paused mode. The CRUD handlers
    only touch the scheduler via ``add_job`` / ``remove_job`` /
    ``get_jobs`` / ``get_job``, all of which behave identically across
    APScheduler subclasses, so the swap is transparent to the code
    under test while letting the scheduler actually start (which is
    what activates ``replace_existing=True`` deduplication).
    """
    import autonomous_agents.scheduler as scheduler_mod

    # Reset singletons so each test starts from a clean slate.
    scheduler_mod._scheduler = BackgroundScheduler(timezone="UTC")
    scheduler_mod._scheduler.start(paused=True)
    scheduler_mod._run_store = None
    tasks_route._task_store = _DictTaskStore()
    webhooks_route._webhook_tasks = {}

    app = FastAPI()
    app.include_router(tasks_route.router, prefix="/api/v1")

    with TestClient(app) as tc:
        yield tc

    if scheduler_mod._scheduler is not None and scheduler_mod._scheduler.running:
        scheduler_mod._scheduler.shutdown(wait=False)
    scheduler_mod._scheduler = None
    tasks_route._task_store = None
    webhooks_route._webhook_tasks = {}


def _cron_task(task_id: str = "t1", *, enabled: bool = True) -> dict:
    return {
        "id": task_id,
        "name": f"Task {task_id}",
        "agent": "github",
        "prompt": "do the thing",
        "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        "enabled": enabled,
    }


def _interval_task(task_id: str = "t1", *, seconds: int = 30) -> dict:
    return {
        "id": task_id,
        "name": f"Task {task_id}",
        "agent": "github",
        "prompt": "do the thing",
        "trigger": {"type": "interval", "seconds": seconds},
        "enabled": True,
    }


def _webhook_task(task_id: str = "hook1", *, secret: str | None = None) -> dict:
    payload = {
        "id": task_id,
        "name": f"Webhook {task_id}",
        "agent": "github",
        "prompt": "respond",
        "trigger": {"type": "webhook"},
        "enabled": True,
    }
    if secret is not None:
        payload["trigger"]["secret"] = secret
    return payload


# --- list / get ---------------------------------------------------------


def test_list_tasks_initially_empty(client: TestClient):
    response = client.get("/api/v1/tasks")
    assert response.status_code == 200
    assert response.json() == []


def test_get_task_404_for_unknown_id(client: TestClient):
    response = client.get("/api/v1/tasks/ghost")
    assert response.status_code == 404


# --- create -------------------------------------------------------------


def test_create_task_returns_201_and_serialized_payload(client: TestClient):
    response = client.post("/api/v1/tasks", json=_cron_task("cron-1"))

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "cron-1"
    assert body["name"] == "Task cron-1"
    assert body["trigger"]["type"] == "cron"
    assert body["enabled"] is True
    # Serializer must include every field the UI form needs to render
    # the edit dialog without a second round-trip.
    for required in ("agent", "prompt", "llm_provider", "timeout_seconds", "max_retries"):
        assert required in body


def test_create_task_registers_with_scheduler(client: TestClient):
    """Hot-reload contract: a freshly-created cron task must show up
    as an APScheduler job. Otherwise the schedule wouldn't fire until
    the next service restart."""
    client.post("/api/v1/tasks", json=_cron_task("cron-1"))

    job_ids = [j.id for j in get_scheduler().get_jobs()]
    assert job_ids == ["cron-1"]


def test_create_task_with_webhook_trigger_registers_in_webhook_table(client: TestClient):
    client.post("/api/v1/tasks", json=_webhook_task("hook1"))

    assert "hook1" in webhooks_route._webhook_tasks
    # ...and conversely, must NOT show up in the APScheduler jobstore.
    assert get_scheduler().get_jobs() == []


def test_create_task_with_disabled_flag_skips_scheduler(client: TestClient):
    """Disabled tasks must persist (so the UI can render them) but
    must NOT be scheduled -- toggling enabled=false from the UI is
    operationally meaningful."""
    response = client.post(
        "/api/v1/tasks", json=_cron_task("dis-1", enabled=False)
    )
    assert response.status_code == 201
    assert get_scheduler().get_jobs() == []
    # But it must still be readable via list.
    listed = client.get("/api/v1/tasks").json()
    assert [t["id"] for t in listed] == ["dis-1"]


def test_create_task_returns_409_for_duplicate_id(client: TestClient):
    client.post("/api/v1/tasks", json=_cron_task("t1"))
    response = client.post("/api/v1/tasks", json=_cron_task("t1"))
    assert response.status_code == 409
    # The store must not have been mutated -- still exactly one task.
    listed = client.get("/api/v1/tasks").json()
    assert len(listed) == 1


def test_create_task_returns_422_for_unknown_trigger_type(client: TestClient):
    bad = _cron_task("t1")
    bad["trigger"] = {"type": "smoke-signal"}  # not in the discriminated union
    response = client.post("/api/v1/tasks", json=bad)
    # The TaskDefinition.trigger field is a discriminated union over
    # cron/interval/webhook -- pydantic rejects any other ``type`` at
    # validation time so the bad payload never reaches the store.
    assert response.status_code == 422


def test_create_task_returns_422_for_missing_required_field(client: TestClient):
    """Spec #099 FR-001: ``agent`` is now optional (it's a routing hint,
    not a required field). The remaining truly-required fields are
    still validated -- this test now asserts on ``prompt`` which is
    still mandatory for any meaningful task definition.
    """
    bad = _cron_task("t1")
    del bad["prompt"]  # still required
    response = client.post("/api/v1/tasks", json=bad)
    assert response.status_code == 422


def test_create_task_succeeds_when_agent_omitted(client: TestClient):
    """Spec #099 FR-001 / OQ-1: omitting ``agent`` must succeed and
    persist as null. The supervisor's LLM router will pick a sub-agent
    from the prompt at run time.
    """
    body = _cron_task("t-no-agent")
    del body["agent"]
    response = client.post("/api/v1/tasks", json=body)
    assert response.status_code == 201
    payload = response.json()
    assert payload["agent"] is None


# --- update -------------------------------------------------------------


def test_update_task_replaces_definition_and_re_syncs_scheduler(client: TestClient):
    client.post("/api/v1/tasks", json=_cron_task("t1"))

    updated_payload = _cron_task("t1")
    updated_payload["name"] = "Task renamed"
    updated_payload["trigger"]["schedule"] = "0 18 * * *"
    response = client.put("/api/v1/tasks/t1", json=updated_payload)

    assert response.status_code == 200
    assert response.json()["name"] == "Task renamed"
    # New trigger spec actually swapped in -- if PUT silently no-op'd
    # the live trigger, the next_run would still match the original.
    job = get_scheduler().get_job("t1")
    assert job is not None
    # CronTrigger ``__str__`` includes the field values; check the
    # hour switched from 9 to 18.
    assert "hour='18'" in str(job.trigger)


def test_update_task_swap_from_cron_to_webhook_detaches_old_runtime(client: TestClient):
    """Trigger-type swap is the trickiest case: the previous APScheduler
    job MUST be removed, otherwise the cron would keep firing alongside
    the new webhook endpoint."""
    client.post("/api/v1/tasks", json=_cron_task("t1"))
    assert [j.id for j in get_scheduler().get_jobs()] == ["t1"]

    swap = _webhook_task("t1")
    response = client.put("/api/v1/tasks/t1", json=swap)
    assert response.status_code == 200

    assert get_scheduler().get_jobs() == []
    assert "t1" in webhooks_route._webhook_tasks


def test_update_task_swap_from_webhook_to_cron_detaches_webhook(client: TestClient):
    client.post("/api/v1/tasks", json=_webhook_task("t1"))
    assert "t1" in webhooks_route._webhook_tasks

    swap = _cron_task("t1")
    response = client.put("/api/v1/tasks/t1", json=swap)
    assert response.status_code == 200

    assert "t1" not in webhooks_route._webhook_tasks
    assert [j.id for j in get_scheduler().get_jobs()] == ["t1"]


def test_update_task_404_for_unknown_id(client: TestClient):
    response = client.put("/api/v1/tasks/ghost", json=_cron_task("ghost"))
    assert response.status_code == 404


def test_update_task_coerces_id_to_path(client: TestClient):
    """Path id wins over body id -- otherwise a malformed UI request
    could rename a task by PUT-ing to one URL with a different
    body.id, breaking every URL that referenced the original."""
    client.post("/api/v1/tasks", json=_cron_task("t1"))

    body = _cron_task("t1")
    body["id"] = "different"
    response = client.put("/api/v1/tasks/t1", json=body)
    assert response.status_code == 200
    assert response.json()["id"] == "t1"

    # Confirm via list: still one task, still id=t1.
    listed = client.get("/api/v1/tasks").json()
    assert [t["id"] for t in listed] == ["t1"]


def test_update_task_disable_removes_scheduler_job(client: TestClient):
    """Toggling enabled=true -> false on an existing cron task must
    pull the APScheduler entry, even though the task definition
    itself stays in the store. PR #5 review (Copilot+Codex P1)."""
    client.post("/api/v1/tasks", json=_cron_task("t1"))
    assert [j.id for j in get_scheduler().get_jobs()] == ["t1"]

    disabled = _cron_task("t1", enabled=False)
    response = client.put("/api/v1/tasks/t1", json=disabled)
    assert response.status_code == 200

    # The fix in scheduler.register_task explicitly calls
    # unregister_task(task.id) for disabled tasks, so the prior job
    # must be gone. The store still keeps the disabled definition --
    # only the *runtime* schedule disappears.
    assert get_scheduler().get_job("t1") is None
    listed = client.get("/api/v1/tasks").json()
    assert [t["id"] for t in listed] == ["t1"]
    assert listed[0]["enabled"] is False


def test_update_task_re_enable_re_attaches_scheduler_job(client: TestClient):
    """Symmetric to the disable test: flipping enabled back to true
    must re-create the APScheduler job. Without this, a UI operator
    who toggled 'off' then 'on' would silently end up with a task
    that never fires until the next service restart."""
    client.post("/api/v1/tasks", json=_cron_task("t1"))
    client.put("/api/v1/tasks/t1", json=_cron_task("t1", enabled=False))
    assert get_scheduler().get_job("t1") is None

    response = client.put("/api/v1/tasks/t1", json=_cron_task("t1", enabled=True))
    assert response.status_code == 200
    assert [j.id for j in get_scheduler().get_jobs()] == ["t1"]


# --- create rollback (PR #5 review, Codex P2) --------------------------


def test_create_task_rolls_back_when_scheduler_sync_fails(client: TestClient):
    """A malformed cron expression passes pydantic validation (it's
    just a string) but blows up inside ``APSCronTrigger.from_crontab``.
    The route must roll back the persisted row so a subsequent retry
    with a corrected definition doesn't bounce off a 409.

    PR #5 review (Codex P2): without rollback the task sits in the
    store unschedulable while every retry POST returns 409.
    """
    bad = _cron_task("bad-cron")
    bad["trigger"]["schedule"] = "this is not a cron expression"

    response = client.post("/api/v1/tasks", json=bad)
    assert response.status_code == 400
    assert "could not be scheduled" in response.json()["detail"]

    # Store must be empty -- the compensating delete reverted the
    # earlier ``store.create`` so the operator's retry succeeds.
    listed = client.get("/api/v1/tasks").json()
    assert listed == []
    assert get_scheduler().get_jobs() == []

    fixed = _cron_task("bad-cron")  # default schedule "0 9 * * *"
    retry = client.post("/api/v1/tasks", json=fixed)
    assert retry.status_code == 201, "rollback must clear the way for retry"


# --- webhook secret redaction (PR #6 review, Copilot P1) ---------------


def test_create_webhook_task_redacts_secret_in_response(client: TestClient):
    """The HMAC ``secret`` is the symmetric key used to verify
    incoming POSTs -- echoing it back in API responses leaks it into
    devtools, traces, and audit logs. Replace it with ``has_secret``.
    """
    response = client.post(
        "/api/v1/tasks", json=_webhook_task("hook1", secret="super-secret")
    )
    assert response.status_code == 201
    trigger = response.json()["trigger"]
    assert "secret" not in trigger
    assert trigger["has_secret"] is True


def test_list_and_get_webhook_task_never_echo_secret(client: TestClient):
    client.post("/api/v1/tasks", json=_webhook_task("hook1", secret="s"))

    listed = client.get("/api/v1/tasks").json()
    assert "secret" not in listed[0]["trigger"]
    assert listed[0]["trigger"]["has_secret"] is True

    fetched = client.get("/api/v1/tasks/hook1").json()
    assert "secret" not in fetched["trigger"]
    assert fetched["trigger"]["has_secret"] is True


def test_webhook_task_without_secret_reports_has_secret_false(client: TestClient):
    client.post("/api/v1/tasks", json=_webhook_task("hook1"))  # no secret
    fetched = client.get("/api/v1/tasks/hook1").json()
    assert fetched["trigger"]["has_secret"] is False


# --- webhook secret preservation on PUT --------------------------------


def test_update_preserves_existing_secret_when_omitted(client: TestClient):
    """The UI never receives the secret on GET (redacted), so it can't
    echo it back on PUT. A PUT with no secret must therefore mean
    'keep what's already there', not 'wipe the secret'."""
    client.post("/api/v1/tasks", json=_webhook_task("hook1", secret="original-secret"))

    update = _webhook_task("hook1")  # secret omitted
    response = client.put("/api/v1/tasks/hook1", json=update)
    assert response.status_code == 200
    assert response.json()["trigger"]["has_secret"] is True

    # Verify directly against the store -- the secret is intact.
    stored = tasks_route._task_store
    assert stored is not None
    import asyncio

    task = asyncio.run(stored.get("hook1"))
    assert task is not None
    assert task.trigger.secret == "original-secret"


def test_update_can_explicitly_replace_secret(client: TestClient):
    """The other direction: when the UI submits a NEW secret value,
    we must take it. Otherwise rotation is impossible."""
    client.post("/api/v1/tasks", json=_webhook_task("hook1", secret="old"))

    update = _webhook_task("hook1", secret="new-secret")
    response = client.put("/api/v1/tasks/hook1", json=update)
    assert response.status_code == 200

    stored = tasks_route._task_store
    assert stored is not None
    import asyncio

    task = asyncio.run(stored.get("hook1"))
    assert task is not None
    assert task.trigger.secret == "new-secret"


# --- delete -------------------------------------------------------------


def test_delete_task_removes_from_store_and_scheduler(client: TestClient):
    client.post("/api/v1/tasks", json=_cron_task("t1"))

    response = client.delete("/api/v1/tasks/t1")
    assert response.status_code == 204

    # Empty list, no scheduler job, no webhook registration.
    assert client.get("/api/v1/tasks").json() == []
    assert get_scheduler().get_jobs() == []


def test_delete_task_removes_webhook_registration(client: TestClient):
    client.post("/api/v1/tasks", json=_webhook_task("hook1"))
    assert "hook1" in webhooks_route._webhook_tasks

    response = client.delete("/api/v1/tasks/hook1")
    assert response.status_code == 204
    assert "hook1" not in webhooks_route._webhook_tasks


def test_delete_task_404_for_unknown_id(client: TestClient):
    """``rm`` semantics, not ``rm -f`` -- the UI surfaces a clear
    error when two operators race a delete instead of pretending
    everything succeeded."""
    response = client.delete("/api/v1/tasks/ghost")
    assert response.status_code == 404


def test_round_trip_create_get_update_delete(client: TestClient):
    """Sanity smoke covering the full UI flow in one shot."""
    # CREATE
    create = client.post("/api/v1/tasks", json=_interval_task("t1", seconds=15))
    assert create.status_code == 201

    # GET
    got = client.get("/api/v1/tasks/t1")
    assert got.status_code == 200
    assert got.json()["trigger"]["seconds"] == 15

    # UPDATE
    updated_payload = _interval_task("t1", seconds=60)
    updated = client.put("/api/v1/tasks/t1", json=updated_payload)
    assert updated.status_code == 200
    assert updated.json()["trigger"]["seconds"] == 60

    # DELETE
    deleted = client.delete("/api/v1/tasks/t1")
    assert deleted.status_code == 204

    # GET -> 404
    assert client.get("/api/v1/tasks/t1").status_code == 404
