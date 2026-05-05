# Copyright 2025 CNOE Contributors
# SPDX-License-Identifier: Apache-2.0

"""Autonomous-task management tools for the supervisor.

These tools wrap the autonomous-agents REST API so the supervisor can
list, create, update, delete, and trigger scheduled work from chat.
They return operator-facing strings instead of raising, which lets the
LLM report validation, transport, and service errors directly.
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
from typing import Any, Literal

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _autonomous_agents_url() -> str:
    """Internal base URL used for service-to-service API calls."""
    return (os.environ.get("AUTONOMOUS_AGENTS_URL") or "http://localhost:8002").rstrip("/")


def _public_base_url() -> str:
    """Public base URL used for external webhook callback URLs."""
    public = os.environ.get("AUTONOMOUS_AGENTS_PUBLIC_URL")
    if public:
        return public.rstrip("/")
    return _autonomous_agents_url()


def _webhook_callback_url(task_id: str) -> str:
    """Build the externally reachable callback URL for a webhook task."""
    base = _public_base_url()
    if base.endswith("/api/v1"):
        return f"{base}/hooks/{task_id}"
    return f"{base}/api/v1/hooks/{task_id}"


def _api_url(path: str) -> str:
    """Compose ``<base>/api/v1/<path>``, never producing a double slash."""
    return f"{_autonomous_agents_url()}/api/v1/{path.lstrip('/')}"


def _format_http_error(exc: httpx.HTTPStatusError) -> str:
    """Pull the FastAPI ``detail`` field out of an error response and format it."""
    try:
        body = exc.response.json()
        detail = body.get("detail") if isinstance(body, dict) else None
    except (ValueError, AttributeError):
        detail = None
    msg = detail if isinstance(detail, str) else exc.response.text or str(exc)
    return f"HTTP {exc.response.status_code}: {msg}"


def _format_task(task: dict[str, Any]) -> str:
    """One-line summary of a task for inclusion in tool responses."""
    trig = task.get("trigger") or {}
    trig_type = trig.get("type", "?")
    trig_summary = ""
    if trig_type == "cron":
        trig_summary = f"cron `{trig.get('schedule', '?')}`"
    elif trig_type == "interval":
        parts = []
        for unit, val in (
            ("h", trig.get("hours")),
            ("m", trig.get("minutes")),
            ("s", trig.get("seconds")),
        ):
            if val:
                parts.append(f"{val}{unit}")
        trig_summary = "every " + " ".join(parts) if parts else "interval (?)"
    elif trig_type == "webhook":
        trig_summary = f"webhook -> POST /api/v1/hooks/{task.get('id')}"
    enabled = "enabled" if task.get("enabled") else "DISABLED"
    next_run = task.get("next_run") or "-"
    return (
        f"- `{task.get('id')}` ({task.get('name')}) "
        f"agent={task.get('agent') or '(LLM-routed)'} "
        f"trigger={trig_summary} "
        f"status={enabled} next_run={next_run}"
    )


@tool
def list_autonomous_tasks() -> str:
    """List every autonomous task currently registered in the scheduler.

    Use this before creating a new task or when the operator asks what
    autonomous work is already scheduled.
    """
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(_api_url("tasks"))
            response.raise_for_status()
        tasks = response.json()
    except httpx.HTTPStatusError as exc:
        return f"Failed to list tasks: {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Autonomous-agents service unreachable at {_autonomous_agents_url()}: {exc}"

    if not isinstance(tasks, list) or not tasks:
        return "No autonomous tasks are configured yet."
    return f"{len(tasks)} autonomous task(s):\n" + "\n".join(_format_task(t) for t in tasks)


@tool
def create_autonomous_task(
    id: str,
    name: str,
    prompt: str,
    trigger_type: Literal["cron", "interval", "webhook"],
    trigger_schedule: str | None = None,
    trigger_seconds: int | None = None,
    trigger_minutes: int | None = None,
    trigger_hours: int | None = None,
    webhook_secret: str | None = None,
    description: str | None = None,
    agent: str | None = None,
    llm_provider: str | None = None,
    enabled: bool = True,
) -> str:
    """Create a new autonomous task.

    Confirm the id, name, trigger, and prompt with the operator before
    calling this tool. For webhook tasks, the response includes the
    callback URL and signing secret needed by external senders such as
    GitHub.

    Args:
        id: Unique short identifier. Used in URLs and as a stable handle.
        name: Human-readable name shown in the UI list.
        prompt: Full prompt sent to the supervisor whenever the task fires.
        trigger_type: ``cron``, ``interval``, or ``webhook``.
        trigger_schedule: 5-field UTC cron expression for cron tasks.
        trigger_seconds: Interval period in seconds.
        trigger_minutes: Interval period in minutes.
        trigger_hours: Interval period in hours.
        webhook_secret: Optional HMAC-SHA256 secret for webhook tasks.
        description: Optional free-form description shown in the UI.
        agent: Optional sub-agent routing hint, e.g. ``github`` or ``argocd``.
        llm_provider: Optional LLM provider override for this task.
        enabled: Whether the task should fire on its schedule.
    """
    trigger: dict[str, Any] = {"type": trigger_type}
    if trigger_type == "cron":
        if not trigger_schedule:
            return (
                "create_autonomous_task: ``trigger_schedule`` is required when "
                "``trigger_type='cron'`` (5-field UTC cron, e.g. '0 9 * * 1-5')."
            )
        trigger["schedule"] = trigger_schedule
    elif trigger_type == "interval":
        if not (trigger_seconds or trigger_minutes or trigger_hours):
            return (
                "create_autonomous_task: provide one of trigger_seconds, "
                "trigger_minutes, or trigger_hours (positive int) when "
                "``trigger_type='interval'``."
            )
        if trigger_seconds:
            trigger["seconds"] = trigger_seconds
        if trigger_minutes:
            trigger["minutes"] = trigger_minutes
        if trigger_hours:
            trigger["hours"] = trigger_hours
    elif trigger_type == "webhook":
        if webhook_secret:
            secret_value = webhook_secret
            secret_was_generated = False
        else:
            secret_value = _secrets.token_hex(32)
            secret_was_generated = True
        trigger["secret"] = secret_value

    payload: dict[str, Any] = {
        "id": id,
        "name": name,
        "prompt": prompt,
        "trigger": trigger,
        "enabled": enabled,
    }
    if description:
        payload["description"] = description
    if agent:
        payload["agent"] = agent
    if llm_provider:
        payload["llm_provider"] = llm_provider

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(_api_url("tasks"), json=payload)
            response.raise_for_status()
        created = response.json()
    except httpx.HTTPStatusError as exc:
        return f"Failed to create task '{id}': {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Autonomous-agents service unreachable at {_autonomous_agents_url()}: {exc}"

    lines = [f"Created autonomous task:\n{_format_task(created)}"]

    if trigger_type == "webhook":
        callback_url = _webhook_callback_url(created.get("id") or id)
        lines.append("")
        lines.append(f"callback_url: {callback_url}")
        if secret_was_generated:
            lines.append(
                f"secret: {secret_value}  "
                "(auto-generated; pass this to register_github_webhook)"
            )
        else:
            lines.append("secret: (using the value you supplied)")
        if not os.environ.get("AUTONOMOUS_AGENTS_PUBLIC_URL"):
            lines.append(
                "Note: AUTONOMOUS_AGENTS_PUBLIC_URL is not set, so the "
                "callback_url above is internal-only. External senders "
                "like GitHub will not reach it. Set "
                "AUTONOMOUS_AGENTS_PUBLIC_URL to the machine's public "
                "hostname and re-create the webhook on GitHub."
            )
    return "\n".join(lines)


@tool
def update_autonomous_task(
    id: str,
    name: str | None = None,
    prompt: str | None = None,
    trigger_type: Literal["cron", "interval", "webhook"] | None = None,
    trigger_schedule: str | None = None,
    trigger_seconds: int | None = None,
    trigger_minutes: int | None = None,
    trigger_hours: int | None = None,
    description: str | None = None,
    agent: str | None = None,
    llm_provider: str | None = None,
    enabled: bool | None = None,
) -> str:
    """Update an existing autonomous task."""
    try:
        with httpx.Client(timeout=10.0) as client:
            existing_resp = client.get(_api_url(f"tasks/{id}"))
            existing_resp.raise_for_status()
        existing = existing_resp.json()
    except httpx.HTTPStatusError as exc:
        return f"Cannot update task '{id}': {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Autonomous-agents service unreachable: {exc}"

    payload = dict(existing)
    if name is not None:
        payload["name"] = name
    if prompt is not None:
        payload["prompt"] = prompt
    if description is not None:
        payload["description"] = description
    if agent is not None:
        payload["agent"] = agent
    if llm_provider is not None:
        payload["llm_provider"] = llm_provider
    if enabled is not None:
        payload["enabled"] = enabled
    if trigger_type is not None:
        new_trig: dict[str, Any] = {"type": trigger_type}
        if trigger_type == "cron":
            new_trig["schedule"] = trigger_schedule or (existing.get("trigger") or {}).get("schedule")
        elif trigger_type == "interval":
            for k, v in (
                ("seconds", trigger_seconds),
                ("minutes", trigger_minutes),
                ("hours", trigger_hours),
            ):
                if v is not None:
                    new_trig[k] = v
        payload["trigger"] = new_trig

    for k in ("last_ack", "chat_conversation_id", "next_run"):
        payload.pop(k, None)

    try:
        with httpx.Client(timeout=15.0) as client:
            put_resp = client.put(_api_url(f"tasks/{id}"), json=payload)
            put_resp.raise_for_status()
        updated = put_resp.json()
    except httpx.HTTPStatusError as exc:
        return f"Failed to update task '{id}': {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Autonomous-agents service unreachable: {exc}"

    return f"Updated autonomous task:\n{_format_task(updated)}"


@tool
def delete_autonomous_task(id: str) -> str:
    """Delete an autonomous task by id after operator confirmation."""
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.delete(_api_url(f"tasks/{id}"))
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Failed to delete task '{id}': {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Autonomous-agents service unreachable: {exc}"
    return f"Deleted autonomous task '{id}'."


@tool
def trigger_autonomous_task_now(id: str) -> str:
    """Run an autonomous task immediately, outside its schedule."""
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(_api_url(f"tasks/{id}/run"))
            response.raise_for_status()
        body = response.json()
    except httpx.HTTPStatusError as exc:
        return f"Failed to trigger task '{id}': {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Autonomous-agents service unreachable: {exc}"
    return f"Queued ad-hoc run of '{id}': {body}"


@tool
def validate_cron_expression(expression: str) -> str:
    """Sanity-check a 5-field cron expression before using it in a task."""
    parts = expression.strip().split()
    if len(parts) != 5:
        return (
            f"Cron expression must have exactly 5 fields "
            f"(minute hour day-of-month month day-of-week); got {len(parts)}: '{expression}'"
        )
    try:
        from apscheduler.triggers.cron import CronTrigger

        CronTrigger.from_crontab(expression)
    except ImportError:
        return f"OK '{expression}' (parsed as 5 fields; APScheduler validation skipped)"
    except (ValueError, TypeError) as exc:
        return f"Invalid cron expression '{expression}': {exc}"
    return f"OK '{expression}'"


__all__ = [
    "list_autonomous_tasks",
    "create_autonomous_task",
    "update_autonomous_task",
    "delete_autonomous_task",
    "trigger_autonomous_task_now",
    "validate_cron_expression",
]
