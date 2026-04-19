# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Task management endpoints -- CRUD, run history, manual trigger.

The :class:`TaskStore` (in-memory or MongoDB-backed) is the single
source of truth for task definitions. Every mutation here goes through
the store first, then immediately re-syncs the APScheduler job and the
webhook registry via the hot-reload helpers so changes take effect
without a service restart.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status

from autonomous_agents.models import TaskDefinition, TaskRun
from autonomous_agents.routes.webhooks import (
    register_webhook_task,
    unregister_webhook_task,
)
from autonomous_agents.scheduler import (
    execute_task,
    get_run_store,
    get_scheduler,
    register_task,
    unregister_task,
)
from autonomous_agents.services.task_store import (
    InMemoryTaskStore,
    TaskAlreadyExistsError,
    TaskNotFoundError,
    TaskStore,
)

logger = logging.getLogger("autonomous_agents")

router = APIRouter(tags=["tasks"])

# Maximum runs returned by /tasks/{id}/runs. Matches the legacy
# in-memory cap so existing callers see no behaviour change beyond
# the bug fix in IMP-01; raise this if the UI ever needs deeper
# history in a single round-trip.
_MAX_TASK_RUNS = 500

# Module-level TaskStore singleton. Injected by the FastAPI lifespan
# in ``main.py``; falls back to an in-memory store when accessed before
# injection (e.g. from unit tests that don't spin up the full app).
_task_store: TaskStore | None = None


def get_task_store() -> TaskStore:
    """Return the active :class:`TaskStore`.

    Lazy fallback to :class:`InMemoryTaskStore` mirrors the
    ``get_run_store`` pattern so route-level tests can exercise the
    handlers without running the FastAPI lifespan.
    """
    global _task_store
    if _task_store is None:
        _task_store = InMemoryTaskStore()
    return _task_store


def set_task_store(store: TaskStore) -> None:
    """Inject the active :class:`TaskStore` -- called from the FastAPI lifespan."""
    global _task_store
    _task_store = store


async def _sync_task_to_runtime(task: TaskDefinition) -> None:
    """Reflect a stored task into the live scheduler + webhook registry.

    The CRUD handlers are the only place that should be calling the
    hot-reload helpers, so centralising the dispatch here makes it
    impossible for a future endpoint to update one and forget the
    other. Both helpers are idempotent and skip non-matching trigger
    types, so calling them unconditionally is safe and keeps
    enable/disable toggles from leaving stale entries behind.
    """
    register_task(task)
    register_webhook_task(task)


def _detach_task_from_runtime(task_id: str) -> None:
    """Drop a task from both the scheduler and webhook registry.

    Mirrors :func:`_sync_task_to_runtime` for the delete path. Both
    underlying helpers return a bool rather than raising on
    ``not found``, so this is safe to call for a webhook-only or
    disabled task whose id was never registered with one or the
    other side.
    """
    unregister_task(task_id)
    unregister_webhook_task(task_id)


def _serialize_task(task: TaskDefinition, next_run_iso: str | None) -> dict:
    """Render a task into the wire shape the UI expects.

    Kept as a single helper so list/get/create/update all return the
    exact same structure -- otherwise the React side has to deal with
    "this field shows up on POST responses but not on GET" drift.
    """
    return {
        "id": task.id,
        "name": task.name,
        "description": task.description,
        "agent": task.agent,
        "prompt": task.prompt,
        "llm_provider": task.llm_provider,
        "trigger": task.trigger.model_dump(),
        "enabled": task.enabled,
        "timeout_seconds": task.timeout_seconds,
        "max_retries": task.max_retries,
        "next_run": next_run_iso,
    }


def _next_run_iso_for(task_id: str) -> str | None:
    """Look up the next scheduled fire time for ``task_id``.

    Returns ``None`` for webhook-only / disabled / unknown tasks so the
    UI can render "no upcoming run" without a separate code path.
    """
    job = get_scheduler().get_job(task_id)
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.isoformat()


@router.get("/tasks", response_model=list[dict])
async def list_tasks() -> list[dict]:
    """List all configured tasks plus their next scheduled run time."""
    tasks = await get_task_store().list_all()
    return [_serialize_task(t, _next_run_iso_for(t.id)) for t in tasks]


@router.get("/tasks/{task_id}", response_model=dict)
async def get_task(task_id: str) -> dict:
    """Return a single task definition (used by the UI edit form)."""
    task = await get_task_store().get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return _serialize_task(task, _next_run_iso_for(task_id))


@router.post("/tasks", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_task(task: TaskDefinition) -> dict:
    """Create a new task definition.

    On success the task is immediately wired into the scheduler /
    webhook registry. A 409 is returned for duplicate ids rather than
    silently overwriting -- update goes through PUT.
    """
    try:
        created = await get_task_store().create(task)
    except TaskAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await _sync_task_to_runtime(created)
    logger.info(f"[{created.id}] Created via API")
    return _serialize_task(created, _next_run_iso_for(created.id))


@router.put("/tasks/{task_id}", response_model=dict)
async def update_task(task_id: str, task: TaskDefinition) -> dict:
    """Replace an existing task definition.

    The path id wins on conflict -- a body that disagrees gets coerced
    so callers can't accidentally rename a task by PUT-ing to one URL
    with a different ``id`` field. Hot-reloads the scheduler so the
    new trigger spec takes effect on its next fire.
    """
    if task.id != task_id:
        # Coerce rather than 400 -- the UI typically renders the id as
        # immutable text, but we don't want to trust that contract.
        task = task.model_copy(update={"id": task_id})

    store = get_task_store()
    # Capture the previous trigger type *before* committing the update.
    # We need this to know whether the update is a trigger-type swap
    # (e.g. cron -> webhook), in which case the old runtime entry on
    # the *other* side has to be explicitly torn down. ``existing`` is
    # ``None`` for unknown ids -- the store update call below will
    # then raise TaskNotFoundError and we 404 cleanly.
    existing = await store.get(task_id)

    try:
        updated = await store.update(task_id, task)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Trigger-type swap: explicitly drop the old runtime entry so e.g.
    # a former webhook task doesn't keep accepting POSTs alongside the
    # new cron. Same-type updates rely on ``register_task``'s
    # ``replace_existing=True`` and ``register_webhook_task``'s
    # in-place dict overwrite, both of which are atomic.
    if existing is not None and existing.trigger.type != updated.trigger.type:
        _detach_task_from_runtime(task_id)
    await _sync_task_to_runtime(updated)
    logger.info(f"[{updated.id}] Updated via API")
    return _serialize_task(updated, _next_run_iso_for(updated.id))


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: str) -> None:
    """Delete a task definition and detach it from the scheduler.

    Returns 204 on success, 404 if the task was already gone -- POSIX
    ``rm`` semantics rather than idempotent ``rm -f`` because the UI
    needs to be able to surface "this task no longer exists" if two
    operators are deleting concurrently.
    """
    try:
        await get_task_store().delete(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _detach_task_from_runtime(task_id)
    logger.info(f"[{task_id}] Deleted via API")


@router.get("/tasks/{task_id}/runs", response_model=list[TaskRun])
async def get_task_runs(task_id: str) -> list[TaskRun]:
    """Return run history for a specific task."""
    # Pre-IMP-01 the in-memory deque retained up to 500 runs across
    # all tasks and this endpoint returned every match. Calling
    # ``list_by_task(task_id)`` with the protocol's default ``limit=100``
    # silently truncated history for any task with more than 100 past
    # runs -- a regression. Pass an explicit cap so behaviour matches
    # the legacy contract regardless of which RunStore is active.
    history = await get_run_store().list_by_task(task_id, limit=_MAX_TASK_RUNS)
    if history:
        return history
    # Only 404 when there is BOTH no history AND no current task
    # definition. This keeps the endpoint useful for inspecting runs
    # of tasks whose definition was deleted from config.yaml.
    if await get_task_store().get(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return history


@router.post("/tasks/{task_id}/run", response_model=dict)
async def trigger_task_manually(task_id: str) -> dict:
    """Manually trigger a task to run immediately (for testing)."""
    task = await get_task_store().get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    # Fire-and-forget -- the run is recorded in the store as it
    # progresses so the UI can poll /tasks/{id}/runs to see the result.
    asyncio.create_task(execute_task(task))
    return {"status": "triggered", "task_id": task_id}


@router.get("/runs", response_model=list[TaskRun])
async def list_all_runs() -> list[TaskRun]:
    """Return the full run history across all tasks."""
    return await get_run_store().list_all()
