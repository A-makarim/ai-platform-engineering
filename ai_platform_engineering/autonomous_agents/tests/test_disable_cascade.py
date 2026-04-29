# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Tests for the dynamic-agent cascade-disable flow.

Two contracts live here:

1. ``POST /tasks/disable-by-dynamic-agent/{id}`` flips every matching
   task to ``enabled=false`` with a stable ``disabled_reason`` marker
   and detaches it from the runtime registries. Idempotent on re-call.
2. ``PUT /tasks/{id}`` blocks a re-enable while the cascade marker is
   set (with a synchronous preflight against the dynamic-agents
   service), and clears the marker on a retarget or successful probe.

The cascade is what stops a deleted custom agent from leaving zombie
tasks firing into a 404 every minute, so a regression here would
re-introduce the exact "ack failed always" symptom these tests guard
against.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autonomous_agents.models import TaskDefinition
from autonomous_agents.routes import tasks as tasks_route
from autonomous_agents.routes import webhooks as webhooks_route
from autonomous_agents.services.mongo import (
    TaskAlreadyExistsError,
    TaskNotFoundError,
)
from autonomous_agents.services.preflight import Acknowledgement


class _DictTaskStore:
    """Same minimal Protocol fake the other route tests use."""

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
    """Same wiring pattern as test_tasks_crud_route.client."""
    import autonomous_agents.scheduler as scheduler_mod

    scheduler_mod._scheduler = BackgroundScheduler(timezone="UTC")
    scheduler_mod._scheduler.start(paused=True)
    scheduler_mod._run_store = None
    tasks_route._task_store = _DictTaskStore()
    webhooks_route._webhook_tasks = {}

    app = FastAPI()
    app.include_router(tasks_route.router, prefix="/api/v1")

    with TestClient(app) as tc:
        yield tc

    if (
        scheduler_mod._scheduler is not None
        and scheduler_mod._scheduler.running
    ):
        scheduler_mod._scheduler.shutdown(wait=False)
    scheduler_mod._scheduler = None
    tasks_route._task_store = None
    webhooks_route._webhook_tasks = {}


def _ok_ack() -> Acknowledgement:
    return Acknowledgement(
        ack_status="ok",
        ack_detail="Dynamic agent reachable.",
        routed_to="agent-x",
        tools=[],
        available_agents=[],
        credentials_status={},
        dry_run_summary="ok",
        ack_at=datetime.now(timezone.utc),
    )


def _failed_ack(detail: str = "Agent not found") -> Acknowledgement:
    return Acknowledgement(
        ack_status="failed",
        ack_detail=detail,
        routed_to=None,
        tools=[],
        available_agents=[],
        credentials_status={},
        dry_run_summary="",
        ack_at=datetime.now(timezone.utc),
    )


def _custom_agent_task_payload(
    task_id: str, *, dynamic_agent_id: str = "agent-x", enabled: bool = True
) -> dict:
    return {
        "id": task_id,
        "name": f"Task {task_id}",
        "dynamic_agent_id": dynamic_agent_id,
        "prompt": "do the thing",
        "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        "enabled": enabled,
    }


# ---------------------------------------------------------------------------
# Cascade endpoint
# ---------------------------------------------------------------------------


def test_cascade_disables_matching_tasks_and_marks_them(client: TestClient):
    """Happy path: every task pointing at the deleted agent is disabled
    and gets the ``custom_agent_deleted`` marker. Tasks for unrelated
    agents are left alone."""
    # Two custom-agent tasks bound to the agent we're about to "delete"
    # plus one bound to a different custom agent and one supervisor task
    # to prove the filter is precise.
    client.post("/api/v1/tasks", json=_custom_agent_task_payload("t1"))
    client.post("/api/v1/tasks", json=_custom_agent_task_payload("t2"))
    client.post(
        "/api/v1/tasks",
        json=_custom_agent_task_payload("t3", dynamic_agent_id="other-agent"),
    )
    client.post(
        "/api/v1/tasks",
        json={
            "id": "t4",
            "name": "Supervisor task",
            "agent": "github",
            "prompt": "open a PR",
            "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        },
    )

    response = client.post("/api/v1/tasks/disable-by-dynamic-agent/agent-x")
    assert response.status_code == 200
    body = response.json()
    assert body["disabled_count"] == 2
    assert sorted(body["task_ids"]) == ["t1", "t2"]

    # Verify the persisted state.
    listed = {t["id"]: t for t in client.get("/api/v1/tasks").json()}
    assert listed["t1"]["enabled"] is False
    assert listed["t1"]["disabled_reason"] == "custom_agent_deleted"
    assert listed["t2"]["enabled"] is False
    assert listed["t2"]["disabled_reason"] == "custom_agent_deleted"
    # Untouched siblings.
    assert listed["t3"]["enabled"] is True
    assert listed["t3"]["disabled_reason"] is None
    assert listed["t4"]["enabled"] is True
    assert listed["t4"]["disabled_reason"] is None


def test_cascade_unschedules_disabled_tasks(client: TestClient):
    """Cascade must detach the APScheduler job, not just write the doc.

    Otherwise ``register_task``'s ``replace_existing`` could leave a
    stale fire scheduled before the next ``add_job`` call, which is the
    exact zombie behaviour the cascade exists to prevent.
    """
    from autonomous_agents.scheduler import get_scheduler

    client.post("/api/v1/tasks", json=_custom_agent_task_payload("t1"))
    assert get_scheduler().get_job("t1") is not None

    response = client.post("/api/v1/tasks/disable-by-dynamic-agent/agent-x")
    assert response.status_code == 200
    assert get_scheduler().get_job("t1") is None


def test_cascade_is_idempotent(client: TestClient):
    """A retried cascade returns disabled_count=0 and doesn't rewrite docs."""
    client.post("/api/v1/tasks", json=_custom_agent_task_payload("t1"))

    first = client.post("/api/v1/tasks/disable-by-dynamic-agent/agent-x")
    assert first.json()["disabled_count"] == 1

    second = client.post("/api/v1/tasks/disable-by-dynamic-agent/agent-x")
    assert second.status_code == 200
    body = second.json()
    assert body["disabled_count"] == 0
    assert body["task_ids"] == []


def test_cascade_no_match_returns_zero(client: TestClient):
    """Cascade against an id with no tasks is a 200, not a 404."""
    response = client.post("/api/v1/tasks/disable-by-dynamic-agent/ghost")
    assert response.status_code == 200
    assert response.json() == {"disabled_count": 0, "task_ids": []}


# ---------------------------------------------------------------------------
# PUT re-enable guard
# ---------------------------------------------------------------------------


def test_put_blocks_reenable_when_agent_still_missing(client: TestClient):
    """Operator flips ``enabled`` back on while the dynamic agent is
    still gone -> 409 and the marker survives the round-trip."""
    # Seed a cascaded task by going through the public endpoints.
    client.post("/api/v1/tasks", json=_custom_agent_task_payload("t1"))
    client.post("/api/v1/tasks/disable-by-dynamic-agent/agent-x")

    payload = _custom_agent_task_payload("t1", enabled=True)

    da_preflight = AsyncMock(return_value=_failed_ack())
    with patch(
        "autonomous_agents.routes.tasks.preflight_dynamic_agent",
        new=da_preflight,
    ):
        response = client.put("/api/v1/tasks/t1", json=payload)

    assert response.status_code == 409
    assert "no longer available" in response.json()["detail"]
    da_preflight.assert_awaited_once()
    assert da_preflight.await_args.kwargs["agent_id"] == "agent-x"

    # Marker is still pinned; task is still disabled.
    persisted = client.get("/api/v1/tasks/t1").json()
    assert persisted["enabled"] is False
    assert persisted["disabled_reason"] == "custom_agent_deleted"


def test_put_clears_marker_when_agent_resolves(client: TestClient):
    """Operator recreated the agent (same id) and re-enables the task ->
    PUT succeeds, marker cleared, task scheduled again."""
    client.post("/api/v1/tasks", json=_custom_agent_task_payload("t1"))
    client.post("/api/v1/tasks/disable-by-dynamic-agent/agent-x")

    payload = _custom_agent_task_payload("t1", enabled=True)

    da_preflight = AsyncMock(return_value=_ok_ack())
    with patch(
        "autonomous_agents.routes.tasks.preflight_dynamic_agent",
        new=da_preflight,
    ):
        response = client.put("/api/v1/tasks/t1", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["disabled_reason"] is None
    da_preflight.assert_awaited_once()


def test_put_clears_marker_on_retarget_without_preflight(client: TestClient):
    """Operator retargets ``dynamic_agent_id`` (or clears it) -> the
    stale marker is dropped without a synchronous preflight, because
    the marker was specific to the prior routing target."""
    client.post("/api/v1/tasks", json=_custom_agent_task_payload("t1"))
    client.post("/api/v1/tasks/disable-by-dynamic-agent/agent-x")

    # Retarget to a different custom agent. Note: enabled stays false
    # (operator may want to verify the new target before turning it on).
    payload = _custom_agent_task_payload(
        "t1", dynamic_agent_id="agent-y", enabled=False
    )

    da_preflight = AsyncMock(return_value=_ok_ack())
    with patch(
        "autonomous_agents.routes.tasks.preflight_dynamic_agent",
        new=da_preflight,
    ):
        response = client.put("/api/v1/tasks/t1", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["disabled_reason"] is None
    assert body["dynamic_agent_id"] == "agent-y"
    # Re-enable preflight is NOT triggered on a retarget; only the
    # background ack-relevant preflight (which we patched out for
    # determinism) would fire.
    da_preflight.assert_not_awaited()


def test_put_preserves_marker_for_disabled_only_edits(client: TestClient):
    """An operator editing the prompt of a disabled cascaded task should
    NOT lose the badge -- otherwise the next list refresh would render
    a plain 'disabled' chip and hide the cascade context."""
    client.post("/api/v1/tasks", json=_custom_agent_task_payload("t1"))
    client.post("/api/v1/tasks/disable-by-dynamic-agent/agent-x")

    payload = _custom_agent_task_payload("t1", enabled=False)
    payload["prompt"] = "edited prompt"

    response = client.put("/api/v1/tasks/t1", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["disabled_reason"] == "custom_agent_deleted"
    assert body["prompt"] == "edited prompt"
