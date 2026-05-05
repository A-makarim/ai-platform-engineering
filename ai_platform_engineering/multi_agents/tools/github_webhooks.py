# Copyright 2025 CNOE Contributors
# SPDX-License-Identifier: Apache-2.0

"""GitHub webhook management tools for the supervisor.

These tools register, list, delete, and test repository webhooks through
GitHub's REST API. They pair with ``create_autonomous_task``: CAIPE
creates the webhook task, then this module configures GitHub to send
events to that task's callback URL.
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
from typing import Any

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
KNOWN_EVENTS = (
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "push",
    "release",
    "workflow_run",
    "star",
    "fork",
    "*",
)


def _github_token() -> str | None:
    """Return the configured GitHub PAT or ``None`` if unset."""
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    return token.strip() if token else None


def _auth_headers() -> dict[str, str]:
    """Headers for GitHub REST API calls."""
    token = _github_token()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_repo(repo: str) -> tuple[str | None, str | None, str | None]:
    """Parse ``owner/name`` or a GitHub URL into ``(owner, name, error)``."""
    if not repo or not isinstance(repo, str):
        return None, None, "repo must be a non-empty string like 'owner/name'"

    value = repo.strip()
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break

    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[: -len(".git")]

    if "/" not in value:
        return None, None, f"'{repo}' is not in 'owner/name' form"

    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name:
        return None, None, f"'{repo}' is not a valid 'owner/name' identifier"
    return owner, name, None


def _format_http_error(exc: httpx.HTTPStatusError) -> str:
    """Format GitHub API errors with a short remediation hint."""
    status = exc.response.status_code
    message = ""
    try:
        body = exc.response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        message = str(body.get("message") or "").strip()
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            bits: list[str] = []
            for err in errors:
                if isinstance(err, dict):
                    bits.append(
                        " ".join(
                            str(v)
                            for v in (
                                err.get("resource"),
                                err.get("field"),
                                err.get("code"),
                                err.get("message"),
                            )
                            if v
                        )
                    )
                else:
                    bits.append(str(err))
            if bits:
                message = f"{message} ({'; '.join(bits)})" if message else "; ".join(bits)

    if not message:
        message = exc.response.text or str(exc)

    hint = ""
    if status == 401:
        hint = " (Check GITHUB_PERSONAL_ACCESS_TOKEN.)"
    elif status == 403:
        hint = (
            " (Token may lack the 'admin:repo_hook' scope, or the repo "
            "may require SSO authorization for this token.)"
        )
    elif status == 404:
        hint = " (Repo not found, or the token has no access to it.)"
    elif status == 422:
        hint = " (GitHub rejected the webhook config; check for duplicate URLs or bad events.)"

    return f"HTTP {status}: {message}{hint}"


def _format_transport_error(exc: httpx.TransportError) -> str:
    """Format network-layer errors."""
    return (
        "Could not reach GitHub's API "
        f"({exc.__class__.__name__}: {exc}). "
        "Verify the machine has internet connectivity."
    )


def _format_hook(hook: dict[str, Any], repo: str) -> str:
    """One-line summary of a GitHub webhook."""
    hook_id = hook.get("id", "?")
    config = hook.get("config") or {}
    url = config.get("url", "?")
    events = hook.get("events") or []
    events_str = ",".join(events) if events else "(none)"
    state = "active" if hook.get("active", True) else "inactive"
    return f"  hook#{hook_id} on {repo} -> {url}  events=[{events_str}]  {state}"


@tool
def register_github_webhook(
    repo: str,
    callback_url: str,
    events: list[str] | None = None,
    secret: str | None = None,
    active: bool = True,
) -> str:
    """Register a GitHub repo webhook that POSTs to ``callback_url``.

    Args:
        repo: Repository identifier, ``owner/name`` or a GitHub repo URL.
        callback_url: Public HTTP(S) endpoint GitHub should call.
        events: GitHub event names. Defaults to ``["issues"]``.
        secret: Optional HMAC-SHA256 secret. Generated if omitted.
        active: Whether GitHub should deliver events immediately.
    """
    owner, name, err = _parse_repo(repo)
    if err:
        return f"Could not register webhook: {err}"

    token = _github_token()
    if not token:
        return (
            "Could not register webhook: GITHUB_PERSONAL_ACCESS_TOKEN is not "
            "set in the environment. Configure it (with 'admin:repo_hook' "
            "scope) and retry."
        )

    if not callback_url or not isinstance(callback_url, str):
        return "Could not register webhook: callback_url must be a non-empty URL string"

    callback_url = callback_url.strip()
    if not callback_url.startswith(("http://", "https://")):
        return (
            "Could not register webhook: callback_url must start with http(s). "
            f"Got: {callback_url}"
        )

    if events is None or len(events) == 0:
        events = ["issues"]

    if secret is None:
        secret = _secrets.token_hex(32)
        secret_generated = True
    else:
        secret_generated = False

    payload = {
        "name": "web",
        "active": bool(active),
        "events": list(events),
        "config": {
            "url": callback_url,
            "content_type": "json",
            "insecure_ssl": "0",
            "secret": secret,
        },
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{GITHUB_API_URL}/repos/{owner}/{name}/hooks",
                headers=_auth_headers(),
                json=payload,
            )
            resp.raise_for_status()
            hook = resp.json()
    except httpx.HTTPStatusError as exc:
        return f"Could not register webhook on {owner}/{name}: {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Could not register webhook on {owner}/{name}: {_format_transport_error(exc)}"
    except ValueError as exc:
        return (
            f"Webhook registered but GitHub's response was not JSON: {exc}. "
            "Check github.com in the browser to verify."
        )

    hook_id = hook.get("id", "?")
    resolved_url = (hook.get("config") or {}).get("url", callback_url)
    resolved_events = hook.get("events") or events

    lines = [
        f"Webhook registered on {owner}/{name} (hook_id={hook_id}).",
        f"  events: {', '.join(resolved_events)}",
        f"  callback: {resolved_url}",
        f"  signing: HMAC-SHA256 via X-Hub-Signature-256",
    ]
    if secret_generated:
        lines.append(
            f"  (auto-generated secret: {secret} - store it in the "
            "task's trigger config or WEBHOOK_SECRET)"
        )
    return "\n".join(lines)


@tool
def list_github_webhooks(repo: str) -> str:
    """List webhooks currently registered on a GitHub repository."""
    owner, name, err = _parse_repo(repo)
    if err:
        return f"Could not list webhooks: {err}"

    if not _github_token():
        return (
            "Could not list webhooks: GITHUB_PERSONAL_ACCESS_TOKEN is not "
            "set in the environment."
        )

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"{GITHUB_API_URL}/repos/{owner}/{name}/hooks",
                headers=_auth_headers(),
            )
            resp.raise_for_status()
            hooks = resp.json()
    except httpx.HTTPStatusError as exc:
        return f"Could not list webhooks on {owner}/{name}: {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Could not list webhooks on {owner}/{name}: {_format_transport_error(exc)}"
    except ValueError as exc:
        return f"Could not list webhooks on {owner}/{name}: response was not JSON ({exc})"

    if not isinstance(hooks, list):
        return f"Could not list webhooks on {owner}/{name}: unexpected response shape"

    if not hooks:
        return f"No webhooks registered on {owner}/{name}."

    lines = [f"Webhooks on {owner}/{name} ({len(hooks)} total):"]
    for hook in hooks:
        if isinstance(hook, dict):
            lines.append(_format_hook(hook, f"{owner}/{name}"))
    return "\n".join(lines)


@tool
def delete_github_webhook(repo: str, hook_id: int) -> str:
    """Delete a GitHub repo webhook by id."""
    owner, name, err = _parse_repo(repo)
    if err:
        return f"Could not delete webhook: {err}"

    if not _github_token():
        return (
            "Could not delete webhook: GITHUB_PERSONAL_ACCESS_TOKEN is not "
            "set in the environment."
        )

    if not isinstance(hook_id, int) or hook_id <= 0:
        return (
            f"Could not delete webhook: hook_id must be a positive integer, "
            f"got {hook_id!r}. Use list_github_webhooks to find the id."
        )

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.delete(
                f"{GITHUB_API_URL}/repos/{owner}/{name}/hooks/{hook_id}",
                headers=_auth_headers(),
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Could not delete webhook {hook_id} on {owner}/{name}: {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Could not delete webhook {hook_id} on {owner}/{name}: {_format_transport_error(exc)}"

    return f"Webhook {hook_id} deleted from {owner}/{name}."


@tool
def test_github_webhook(repo: str, hook_id: int) -> str:
    """Ask GitHub to send a test ``push`` delivery to an existing webhook."""
    owner, name, err = _parse_repo(repo)
    if err:
        return f"Could not test webhook: {err}"

    if not _github_token():
        return (
            "Could not test webhook: GITHUB_PERSONAL_ACCESS_TOKEN is not "
            "set in the environment."
        )

    if not isinstance(hook_id, int) or hook_id <= 0:
        return f"Could not test webhook: hook_id must be a positive integer, got {hook_id!r}."

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{GITHUB_API_URL}/repos/{owner}/{name}/hooks/{hook_id}/tests",
                headers=_auth_headers(),
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return f"Could not test webhook {hook_id} on {owner}/{name}: {_format_http_error(exc)}"
    except httpx.TransportError as exc:
        return f"Could not test webhook {hook_id} on {owner}/{name}: {_format_transport_error(exc)}"

    return (
        f"Test delivery requested for webhook {hook_id} on {owner}/{name}. "
        "Check the autonomous-agents logs or the 'Recent Deliveries' tab "
        "on the webhook's settings page on GitHub."
    )
