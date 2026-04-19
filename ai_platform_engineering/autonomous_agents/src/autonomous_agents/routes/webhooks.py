"""Webhook trigger endpoints — external systems POST here to fire tasks."""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from autonomous_agents.models import TaskDefinition, TriggerType, WebhookPayload, WebhookTrigger
from autonomous_agents.scheduler import fire_webhook_task

logger = logging.getLogger("autonomous_agents")
router = APIRouter(tags=["webhooks"])

_webhook_tasks: dict[str, TaskDefinition] = {}


def register_webhook_task(task: TaskDefinition) -> None:
    """Index a single webhook task for fast lookup at request time.

    Idempotent: re-registering the same id replaces the prior entry.
    Non-webhook (and disabled) tasks are silently skipped so the CRUD
    endpoints can call this unconditionally without first checking the
    trigger type.
    """
    if task.trigger.type != TriggerType.WEBHOOK:
        return
    if not task.enabled:
        # Disabled webhook tasks must not respond to incoming POSTs --
        # otherwise flipping ``enabled=false`` from the UI would leave a
        # zombie endpoint accepting (and triggering) external traffic.
        # Mirror the un-register path to be safe across re-saves.
        _webhook_tasks.pop(task.id, None)
        return
    _webhook_tasks[task.id] = task
    logger.info(f"Webhook task '{task.id}' registered at POST /hooks/{task.id}")


def unregister_webhook_task(task_id: str) -> bool:
    """Remove ``task_id`` from the webhook registry if present.

    Returns ``True`` if an entry was removed, ``False`` otherwise. Same
    no-raise contract as :func:`scheduler.unregister_task` so the CRUD
    layer can call both unconditionally.
    """
    return _webhook_tasks.pop(task_id, None) is not None


def register_webhook_tasks(tasks: list[TaskDefinition]) -> None:
    """Bulk-register webhook tasks (used by the FastAPI lifespan)."""
    for task in tasks:
        register_webhook_task(task)


@router.post("/hooks/{task_id}")
async def receive_webhook(
    task_id: str,
    request: Request,
    x_hub_signature_256: str | None = Header(None),
) -> dict:
    """Accept an incoming webhook and immediately run the matching task.

    If the task has a secret configured, validates the HMAC-SHA256 signature
    in the X-Hub-Signature-256 header (GitHub-style).
    """
    task = _webhook_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"No webhook task found for id '{task_id}'")

    body = await request.body()

    # Validate HMAC signature if a secret is configured on the trigger
    if not isinstance(task.trigger, WebhookTrigger):
        raise HTTPException(status_code=500, detail=f"Task '{task_id}' is not a webhook task")
    if task.trigger.secret:
        if not x_hub_signature_256:
            raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")
        expected = "sha256=" + hmac.new(
            task.trigger.secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # Parse body as JSON context (best-effort)
    context: dict = {}
    try:
        context = json.loads(body)
    except Exception:
        pass

    payload = WebhookPayload(data=context if isinstance(context, dict) else {})
    run = await fire_webhook_task(task, context=payload.model_dump())

    return {"status": "accepted", "run_id": run.run_id, "task_id": task_id}
