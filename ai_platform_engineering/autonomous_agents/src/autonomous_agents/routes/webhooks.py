"""Webhook trigger endpoints — external systems POST here to fire tasks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from autonomous_agents.config import get_settings
from autonomous_agents.models import (
    FollowUpContext,
    TaskDefinition,
    TriggerType,
    WebhookPayload,
    WebhookTrigger,
)
from autonomous_agents.scheduler import fire_webhook_task, get_run_store

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
        # Ensure disabled webhook tasks cannot still be triggered.
        _webhook_tasks.pop(task.id, None)
        return

    _webhook_tasks[task.id] = task
    logger.info("Webhook task '%s' registered at POST /hooks/%s", task.id, task.id)


def unregister_webhook_task(task_id: str) -> bool:
    """Remove ``task_id`` from the webhook registry if present.

    Returns ``True`` if an entry was removed, ``False`` otherwise. Same
    no-raise contract as :func:`scheduler.unregister_task` so the CRUD
    layer can call both unconditionally.
    """
    return _webhook_tasks.pop(task_id, None) is not None


def _resolve_secret(task: TaskDefinition) -> tuple[str | None, str]:
    """Return ``(secret, source)`` for HMAC validation.

    Per-task ``trigger.secret`` wins; if absent we fall back to the
    service-wide ``WEBHOOK_SECRET`` env var. The ``source`` string is
    intended only for log/audit context — never log the secret itself.
    """
    if isinstance(task.trigger, WebhookTrigger) and task.trigger.secret:
        return task.trigger.secret, "task"

    fallback = get_settings().webhook_secret
    if fallback:
        return fallback, "global"

    return None, "none"


def _validate_timestamp(raw: str | None, window: int) -> float:
    """Parse + range-check the ``X-Webhook-Timestamp`` header.

    ``raw`` must be a Unix epoch (int or float, seconds). Rejects
    requests whose timestamp lies more than ``window`` seconds before
    *or* after ``now``. Returns parsed timestamp on success.
    """
    if not raw:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Webhook-Timestamp header (replay protection enabled)",
        )

    try:
        ts = float(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="X-Webhook-Timestamp must be a numeric epoch"
        ) from exc

    # Reject NaN and infinities (float() can parse them).
    if not math.isfinite(ts):
        raise HTTPException(
            status_code=400, detail="X-Webhook-Timestamp must be a finite number"
        )

    now = time.time()
    if abs(now - ts) > window:
        raise HTTPException(
            status_code=401,
            detail=f"Webhook timestamp outside ±{window}s replay window",
        )

    return ts


def _expected_signature(secret: str, body: bytes, timestamp_header: str | None) -> str:
    """Compute the expected ``sha256=...`` signature.

    If ``timestamp_header`` is provided, sign ``f"{ts}.{body}"``.
    Otherwise, sign the body alone.
    """
    signed = (
        timestamp_header.encode("utf-8") + b"." + body
        if timestamp_header is not None
        else body
    )
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _parse_context(body: bytes) -> dict[str, Any]:
    """Best-effort parse request body into a dict context."""
    if not body:
        return {}

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}

    return data if isinstance(data, dict) else {}


def register_webhook_tasks(tasks: list[TaskDefinition]) -> None:
    """Bulk-register webhook tasks (used by the FastAPI lifespan)."""
    for task in tasks:
        register_webhook_task(task)


@router.post("/hooks/{task_id}")
async def receive_webhook(
    task_id: str,
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
    x_webhook_timestamp: str | None = Header(None),
) -> dict:
    """Accept an incoming webhook and immediately run the matching task."""
    task = _webhook_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"No webhook task found for id '{task_id}'")
    if not isinstance(task.trigger, WebhookTrigger):
        raise HTTPException(status_code=500, detail=f"Task '{task_id}' is not a webhook task")

    body = await request.body()

    secret, source = _resolve_secret(task)
    if secret:
        settings = get_settings()
        replay_window = settings.webhook_replay_window_seconds

        timestamp_for_signing: str | None = None
        if replay_window > 0:
            _validate_timestamp(x_webhook_timestamp, replay_window)
            timestamp_for_signing = x_webhook_timestamp

        if not x_hub_signature_256:
            raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")

        expected = _expected_signature(secret, body, timestamp_for_signing)
        if not hmac.compare_digest(expected, x_hub_signature_256):
            # Do not reveal expected signature in response/logs.
            logger.warning(
                "Webhook signature mismatch for task '%s' (secret_source=%s)",
                task_id,
                source,
            )
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        logger.debug(
            "Webhook signature OK for task '%s' (secret_source=%s)",
            task_id,
            source,
        )

    if (x_github_event or "").lower() == "ping":
        logger.info("Ignoring GitHub ping delivery for webhook task '%s'", task_id)
        return {"status": "ignored", "reason": "github_ping", "task_id": task_id}

    context = _parse_context(body)
    payload = WebhookPayload(data=context)
    run = await fire_webhook_task(task, context=payload.model_dump())

    return {"status": "accepted", "run_id": run.run_id, "task_id": task_id}


async def _verify_followup_signature(
    task: TaskDefinition,
    body: bytes,
    signature: str | None,
    timestamp_header: str | None,
) -> None:
    """Shared HMAC + replay-window check for follow-up requests.

    Same scheme as ``receive_webhook`` so the inbound bridge can use a
    single signing routine for both the initial fire and follow-ups.
    Raises :class:`HTTPException` on any failure; returns ``None`` on
    success. Does nothing when no secret is configured (mirrors the
    behaviour of ``receive_webhook`` for unsigned setups).
    """
    secret, source = _resolve_secret(task)
    if not secret:
        return

    settings = get_settings()
    replay_window = settings.webhook_replay_window_seconds

    timestamp_for_signing: str | None = None
    if replay_window > 0:
        _validate_timestamp(timestamp_header, replay_window)
        timestamp_for_signing = timestamp_header

    if not signature:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")

    expected = _expected_signature(secret, body, timestamp_for_signing)
    if not hmac.compare_digest(expected, signature):
        # Do not reveal expected signature in response/logs.
        logger.warning(
            "Follow-up signature mismatch for task '%s' (secret_source=%s)",
            task.id,
            source,
        )
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


@router.post("/hooks/{task_id}/follow-up")
async def receive_followup(
    task_id: str,
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_webhook_timestamp: str | None = Header(None),
) -> dict:
    """Re-fire an existing webhook task with operator follow-up text.

    Used by inbound bridges (e.g. the Webex bot) to forward an
    in-thread reply back to the task that started the thread. The
    body is a JSON :class:`FollowUpContext`; HMAC validation reuses
    the task's webhook secret so the bridge can sign with the same
    key it uses for the initial fire path.

    The resulting :class:`TaskRun` is linked to its parent via
    ``parent_run_id`` so the chat-thread synthesiser can render a
    single threaded timeline. The route returns 202-style metadata
    immediately rather than streaming the new run's events -- the
    bridge polls ``/tasks/{task_id}/runs`` (or the chat publisher) for
    the terminal state.
    """
    task = _webhook_tasks.get(task_id)
    if not task:
        raise HTTPException(
            status_code=404,
            detail=f"No webhook task found for id '{task_id}'",
        )
    if not isinstance(task.trigger, WebhookTrigger):
        raise HTTPException(
            status_code=500, detail=f"Task '{task_id}' is not a webhook task"
        )

    body = await request.body()
    await _verify_followup_signature(
        task, body, x_hub_signature_256, x_webhook_timestamp
    )

    try:
        parsed = json.loads(body) if body else {}
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="Follow-up body must be valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400, detail="Follow-up body must be a JSON object"
        )

    try:
        follow_up = FollowUpContext.model_validate(parsed)
    except ValueError as exc:
        # Pydantic raises ValidationError (a ValueError subclass) for
        # missing / mistyped fields. Surface the message verbatim so the
        # bridge author can see exactly which field is wrong.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Defensive: confirm the parent run actually belongs to this task
    # so a misrouted follow-up cannot graft itself onto a foreign
    # task's chat thread. We list the task's recent runs (capped at
    # the same value as the /tasks/{id}/runs endpoint) and verify
    # the parent id is in that set; an unknown id 404s, a known id
    # owned by a different task is impossible by construction since
    # we only scan this task's runs. Using list_by_task instead of a
    # bespoke get(run_id) keeps the RunStore protocol unchanged.
    recent = await get_run_store().list_by_task(task_id, limit=500)
    if not any(r.run_id == follow_up.parent_run_id for r in recent):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Parent run '{follow_up.parent_run_id}' not found for "
                f"task '{task_id}'"
            ),
        )

    run = await fire_webhook_task(task, context={}, follow_up=follow_up)
    logger.info(
        "[%s] Follow-up run %s queued (parent=%s, transport=%s)",
        task_id,
        run.run_id,
        follow_up.parent_run_id,
        follow_up.transport or "unknown",
    )
    return {
        "status": "accepted",
        "run_id": run.run_id,
        "task_id": task_id,
        "parent_run_id": follow_up.parent_run_id,
    }
