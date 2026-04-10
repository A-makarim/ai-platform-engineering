"""Task management endpoints — list tasks and their run history."""

from fastapi import APIRouter, HTTPException

from autonomous_agents.models import TaskDefinition, TaskRun
from autonomous_agents.scheduler import _execute_task, get_run_history, get_scheduler

router = APIRouter(tags=["tasks"])

# Populated at startup by main.py
_registered_tasks: list[TaskDefinition] = []


def set_registered_tasks(tasks: list[TaskDefinition]) -> None:
    global _registered_tasks
    _registered_tasks = tasks


@router.get("/tasks", response_model=list[dict])
async def list_tasks() -> list[dict]:
    """List all configured tasks and their next scheduled run time."""
    scheduler = get_scheduler()
    jobs = {job.id: job for job in scheduler.get_jobs()}

    result = []
    for task in _registered_tasks:
        job = jobs.get(task.id)
        result.append({
            "id": task.id,
            "name": task.name,
            "description": task.description,
            "trigger": task.trigger.model_dump(),
            "enabled": task.enabled,
            "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
        })
    return result


@router.get("/tasks/{task_id}/runs", response_model=list[TaskRun])
async def get_task_runs(task_id: str) -> list[TaskRun]:
    """Return run history for a specific task."""
    history = [r for r in get_run_history() if r.task_id == task_id]
    if not history and not any(t.id == task_id for t in _registered_tasks):
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return history


@router.post("/tasks/{task_id}/run", response_model=dict)
async def trigger_task_manually(task_id: str) -> dict:
    """Manually trigger a task to run immediately (for testing)."""
    task = next((t for t in _registered_tasks if t.id == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    import asyncio
    asyncio.create_task(_execute_task(task))
    return {"status": "triggered", "task_id": task_id}


@router.get("/runs", response_model=list[TaskRun])
async def list_all_runs() -> list[TaskRun]:
    """Return the full run history across all tasks."""
    return get_run_history()
