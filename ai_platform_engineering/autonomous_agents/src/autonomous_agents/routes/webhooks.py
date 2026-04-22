"""Webhook trigger endpoints — external systems POST here to fire tasks."""

import hashlib
import hmac
import json
import logging
import math
import time

from fastapi import APIRouter, Header, HTTPException, Request

from autonomous_agents.config import get_settings
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


def _resolve_secret(task: TaskDefinition) -> tuple[str | None, str]:
    """Return ``(secret, source)`` for HMAC validation.

    Per-task ``trigger.secret`` wins; if absent we fall back to the
    service-wide ``WEBHOOK_SECRET`` env var (IMP-03). The ``source``
    string is intended only for log/audit context — never log the
    secret itself.
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
    *or* after ``now`` — the future-side check protects against an
    attacker pre-minting a far-future signature once the secret
    leaks. Returns the parsed timestamp on success.
    """
    if not raw:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Webhook-Timestamp header (replay protection enabled)",
        )
    try:
        ts = float(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="X-Webhook-Timestamp must be a numeric epoch"
        ) from exc
    # ``float()`` happily parses ``nan`` / ``inf`` / ``-inf``. ``nan``
    # silently bypasses the replay-window check below because every
    # comparison with NaN returns ``False``, so ``abs(now - nan) > window``
    # is ``False`` and the request would be accepted (Copilot P1 on PR #7).
    # Reject any non-finite value with the same 400 we use for non-numeric
    # input -- both are "client sent garbage" not "replay attack".
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

    When ``timestamp_header`` is provided we sign ``f"{ts}.{body}"``
    (Slack-style), binding the timestamp into the MAC so an attacker
    cannot tamper with the header. Otherwise we sign the body alone
    (GitHub-style — preserves backward compatibility for senders that
    don't yet emit a timestamp header).
    """
    if timestamp_header is not None:
        signed = timestamp_header.encode("utf-8") + b"." + body
    else:
        signed = body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def register_webhook_tasks(tasks: list[TaskDefinition]) -> None:
    """Bulk-register webhook tasks (used by the FastAPI lifespan)."""
    for task in tasks:
        register_webhook_task(task)


@router.post("/hooks/{task_id}")
async def receive_webhook(
    task_id: str,
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_webhook_timestamp: str | None = Header(None),
) -> dict:
    """Accept an incoming webhook and immediately run the matching task.

    HMAC-SHA256 verification fires when *either* the task carries a
    per-task secret OR ``WEBHOOK_SECRET`` is configured globally
    (IMP-03). Per-task secrets win.

    When ``WEBHOOK_REPLAY_WINDOW_SECONDS > 0`` (IMP-07), signed
    requests must additionally include ``X-Webhook-Timestamp`` and
    the signature is computed over ``f"{ts}.{body}"`` so the
    timestamp can't be tampered with.
    """
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
            # Don't echo the expected signature — that would let a
            # forgery oracle off this endpoint. The ``source`` tag is
            # safe (just "task" / "global") and helps debug "wrong
            # secret in env" mishaps without leaking the secret.
            logger.warning(
                "Webhook signature mismatch for task '%s' (secret_source=%s)",
                task_id,
                source,
            )
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        logger.debug(
            "Webhook signature OK for task '%s' (secret_source=%s)", task_id, source
        )

    context: dict = {}
    try:
        context = json.loads(body)
    except Exception:
        pass

    payload = WebhookPayload(data=context if isinstance(context, dict) else {})
    run = await fire_webhook_task(task, context=payload.model_dump())

    return {"status": "accepted", "run_id": run.run_id, "task_id": task_id}
