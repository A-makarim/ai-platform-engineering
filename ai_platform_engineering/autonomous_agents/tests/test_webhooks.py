# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Tests for the /hooks/{task_id} router.

Covers:
- IMP-03: per-task secret precedence and global ``WEBHOOK_SECRET``
  fallback (with no-secret-anywhere = no-validation behaviour
  preserved).
- IMP-07: optional replay protection guarded by
  ``WEBHOOK_REPLAY_WINDOW_SECONDS`` — when enabled, requests must
  carry ``X-Webhook-Timestamp`` and the HMAC is computed over
  ``f"{ts}.{body}"`` so the timestamp is bound into the MAC.
- The legacy GitHub-style flow (sign body alone, no timestamp) keeps
  working when replay protection is disabled, so existing senders
  do not need code changes.
"""

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from autonomous_agents.config import Settings, get_settings
from autonomous_agents.models import (
    TaskDefinition,
    TaskRun,
    TaskStatus,
    WebhookTrigger,
)
from autonomous_agents.routes import webhooks as webhooks_route
from autonomous_agents.routes.webhooks import (
    register_webhook_task as _register,
)
from autonomous_agents.routes.webhooks import (
    router as webhooks_router,
)

# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


def _make_task(task_id: str = "wh-1", *, secret: str | None = None) -> TaskDefinition:
    return TaskDefinition(
        id=task_id,
        name="webhook task",
        agent="dummy-agent",
        prompt="run the thing",
        trigger=WebhookTrigger(secret=secret),
    )


def _hex_sig(secret: str, body: bytes, timestamp: str | None = None) -> str:
    """Mirror the production signature contract — keep tests honest.

    When ``timestamp`` is provided we sign ``f"{ts}.{body}"`` so the
    timestamp is bound into the MAC (replay-protection mode).
    """
    if timestamp is not None:
        signed = timestamp.encode("utf-8") + b"." + body
    else:
        signed = body
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """Wire up an isolated FastAPI app + reset the webhook registry.

    We don't import ``main.app`` because that would also start the
    scheduler and the supervisor health probe. The webhooks router
    only depends on the ``_webhook_tasks`` dict and ``fire_webhook_task``
    — both of which we control here.
    """
    app = FastAPI()
    app.include_router(webhooks_router, prefix="/api/v1")

    webhooks_route._webhook_tasks.clear()

    captured: dict[str, Any] = {"calls": []}

    async def _fake_fire(task: TaskDefinition, context: dict[str, Any]) -> TaskRun:
        captured["calls"].append((task.id, context))
        return TaskRun(
            run_id="r-1",
            task_id=task.id,
            task_name=task.name,
            status=TaskStatus.SUCCESS,
        )

    monkeypatch.setattr(webhooks_route, "fire_webhook_task", _fake_fire)

    test_client = TestClient(app)
    test_client.captured = captured  # type: ignore[attr-defined]
    yield test_client

    webhooks_route._webhook_tasks.clear()
    get_settings.cache_clear()


def _set_settings(monkeypatch, **overrides: Any) -> Settings:
    """Replace the cached Settings singleton for one test."""
    overrides.setdefault("webhook_replay_window_seconds", 0)
    overrides.setdefault("webhook_secret", None)
    settings = Settings(**overrides)
    monkeypatch.setattr(webhooks_route, "get_settings", lambda: settings)
    return settings


# ---------------------------------------------------------------------------
# IMP-03 — per-task secret + global fallback
# ---------------------------------------------------------------------------


def test_no_secret_anywhere_accepts_unsigned_request(client, monkeypatch):
    _set_settings(monkeypatch)
    _register(_make_task())

    resp = client.post("/api/v1/hooks/wh-1", json={"hello": "world"})

    assert resp.status_code == 200
    assert resp.json()["task_id"] == "wh-1"


def test_per_task_secret_required_when_set(client, monkeypatch):
    _set_settings(monkeypatch)
    _register(_make_task(secret="task-secret"))

    body = json.dumps({"x": 1}).encode()
    sig = _hex_sig("task-secret", body)

    ok = client.post(
        "/api/v1/hooks/wh-1", content=body, headers={"X-Hub-Signature-256": sig}
    )
    assert ok.status_code == 200

    bad = client.post("/api/v1/hooks/wh-1", content=body)
    assert bad.status_code == 401
    assert "Missing X-Hub-Signature-256" in bad.json()["detail"]


def test_global_secret_fallback_used_when_task_has_none(client, monkeypatch):
    _set_settings(monkeypatch, webhook_secret="global-fallback")
    _register(_make_task())  # no per-task secret

    body = b'{"event":"push"}'
    sig = _hex_sig("global-fallback", body)

    resp = client.post(
        "/api/v1/hooks/wh-1", content=body, headers={"X-Hub-Signature-256": sig}
    )
    assert resp.status_code == 200


def test_per_task_secret_wins_over_global(client, monkeypatch):
    _set_settings(monkeypatch, webhook_secret="global-fallback")
    _register(_make_task(secret="task-secret"))

    body = b'{"event":"push"}'
    # Signing with the global secret must fail — task secret takes precedence.
    bad = client.post(
        "/api/v1/hooks/wh-1",
        content=body,
        headers={"X-Hub-Signature-256": _hex_sig("global-fallback", body)},
    )
    assert bad.status_code == 401

    ok = client.post(
        "/api/v1/hooks/wh-1",
        content=body,
        headers={"X-Hub-Signature-256": _hex_sig("task-secret", body)},
    )
    assert ok.status_code == 200


def test_invalid_signature_does_not_leak_expected_value(client, monkeypatch):
    _set_settings(monkeypatch)
    _register(_make_task(secret="s"))

    resp = client.post(
        "/api/v1/hooks/wh-1",
        content=b"{}",
        headers={"X-Hub-Signature-256": "sha256=deadbeef"},
    )

    assert resp.status_code == 401
    detail = resp.json()["detail"]
    # Generic message only — must not echo the expected signature
    # (would otherwise be a forgery oracle).
    assert detail == "Invalid webhook signature"


# ---------------------------------------------------------------------------
# IMP-07 — replay protection
# ---------------------------------------------------------------------------


def test_replay_window_disabled_keeps_github_style_signing(client, monkeypatch):
    """Default config (window=0) must accept the legacy body-only HMAC."""
    _set_settings(monkeypatch, webhook_replay_window_seconds=0)
    _register(_make_task(secret="s"))

    body = b'{"a":1}'
    sig = _hex_sig("s", body)  # no timestamp -> signs body alone

    resp = client.post(
        "/api/v1/hooks/wh-1", content=body, headers={"X-Hub-Signature-256": sig}
    )
    assert resp.status_code == 200


def test_replay_window_enabled_requires_timestamp_header(client, monkeypatch):
    _set_settings(monkeypatch, webhook_replay_window_seconds=300)
    _register(_make_task(secret="s"))

    body = b"{}"
    sig = _hex_sig("s", body)

    resp = client.post(
        "/api/v1/hooks/wh-1", content=body, headers={"X-Hub-Signature-256": sig}
    )
    assert resp.status_code == 401
    assert "X-Webhook-Timestamp" in resp.json()["detail"]


def test_replay_window_enabled_signs_timestamp_dot_body(client, monkeypatch):
    _set_settings(monkeypatch, webhook_replay_window_seconds=300)
    _register(_make_task(secret="s"))

    body = b'{"hello":"world"}'
    ts = str(int(time.time()))
    sig = _hex_sig("s", body, timestamp=ts)

    resp = client.post(
        "/api/v1/hooks/wh-1",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-Webhook-Timestamp": ts},
    )
    assert resp.status_code == 200


def test_replay_window_rejects_too_old_timestamp(client, monkeypatch):
    _set_settings(monkeypatch, webhook_replay_window_seconds=60)
    _register(_make_task(secret="s"))

    body = b"{}"
    old_ts = str(int(time.time()) - 3600)  # 1h in the past
    sig = _hex_sig("s", body, timestamp=old_ts)

    resp = client.post(
        "/api/v1/hooks/wh-1",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-Webhook-Timestamp": old_ts},
    )
    assert resp.status_code == 401
    assert "replay window" in resp.json()["detail"]


def test_replay_window_rejects_far_future_timestamp(client, monkeypatch):
    _set_settings(monkeypatch, webhook_replay_window_seconds=60)
    _register(_make_task(secret="s"))

    body = b"{}"
    future_ts = str(int(time.time()) + 3600)  # 1h ahead — clock skew, but huge
    sig = _hex_sig("s", body, timestamp=future_ts)

    resp = client.post(
        "/api/v1/hooks/wh-1",
        content=body,
        headers={"X-Hub-Signature-256": sig, "X-Webhook-Timestamp": future_ts},
    )
    assert resp.status_code == 401


def test_replay_window_rejects_non_numeric_timestamp(client, monkeypatch):
    _set_settings(monkeypatch, webhook_replay_window_seconds=60)
    _register(_make_task(secret="s"))

    body = b"{}"
    ts = "not-a-number"
    # No real signature — request must fail at timestamp parsing first.
    resp = client.post(
        "/api/v1/hooks/wh-1",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=zz", "X-Webhook-Timestamp": ts},
    )
    assert resp.status_code == 400
    assert "numeric epoch" in resp.json()["detail"]


def test_replay_window_disabled_ignores_timestamp_header(client, monkeypatch):
    """When window=0 the body-only signature must validate even if a
    sender helpfully includes a (then-irrelevant) timestamp header."""
    _set_settings(monkeypatch, webhook_replay_window_seconds=0)
    _register(_make_task(secret="s"))

    body = b'{"a":1}'
    sig = _hex_sig("s", body)  # NOT signing the timestamp

    resp = client.post(
        "/api/v1/hooks/wh-1",
        content=body,
        headers={
            "X-Hub-Signature-256": sig,
            "X-Webhook-Timestamp": str(int(time.time())),
        },
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Misc behavioural guards
# ---------------------------------------------------------------------------


def test_unknown_task_returns_404(client, monkeypatch):
    _set_settings(monkeypatch)
    resp = client.post("/api/v1/hooks/missing", json={})
    assert resp.status_code == 404


def test_disabled_task_unregisters_endpoint(client, monkeypatch):
    _set_settings(monkeypatch)
    task = _make_task()
    _register(task)

    # Sanity: enabled = reachable.
    assert client.post("/api/v1/hooks/wh-1", json={}).status_code == 200

    disabled = task.model_copy(update={"enabled": False})
    _register(disabled)
    assert client.post("/api/v1/hooks/wh-1", json={}).status_code == 404


def test_signature_helper_matches_endpoint_for_body_only(client, monkeypatch):
    """Locks in the contract: the public ``_expected_signature`` helper
    is what the endpoint uses, so library callers can pre-sign with it.
    """
    _set_settings(monkeypatch)
    _register(_make_task(secret="library-secret"))

    body = b'{"id":42}'
    sig = webhooks_route._expected_signature("library-secret", body, None)

    resp = client.post(
        "/api/v1/hooks/wh-1", content=body, headers={"X-Hub-Signature-256": sig}
    )
    assert resp.status_code == 200
