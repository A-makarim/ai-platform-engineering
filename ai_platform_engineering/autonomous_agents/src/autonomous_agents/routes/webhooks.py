"""Webhook trigger endpoints — external systems POST here to fire tasks."""

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from autonomous_agents.models import TaskDefinition, TriggerType, WebhookPayload, WebhookTrigger
from autonomous_agents.scheduler import fire_webhook_task

logger = logging.getLogger("autonomous_agents")
router = APIRouter(tags=["webhooks"])

_webhook_tasks: dict[str, TaskDefinition] = {}


def register_webhook_tasks(tasks: list[TaskDefinition]) -> None:
    """Index webhook tasks by their task id for fast lookup at request time."""
    for task in tasks:
        if task.trigger.type == TriggerType.WEBHOOK:
            _webhook_tasks[task.id] = task
            logger.info(f"Webhook task '{task.id}' registered at POST /hooks/{task.id}")


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
    assert isinstance(task.trigger, WebhookTrigger)
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
        import json
        context = json.loads(body)
    except Exception:
        pass

    payload = WebhookPayload(data=context if isinstance(context, dict) else {})
    run = await fire_webhook_task(task, context=payload.model_dump())

    return {"status": "accepted", "run_id": run.run_id, "task_id": task_id}
