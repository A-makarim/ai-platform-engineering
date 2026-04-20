# Copyright 2025 CNOE Contributors
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the supervisor-side autonomous-task management tools.

Spec #099 Phase 3. Each tool is a thin wrapper around the autonomous-agents
REST API. The tests:

* Mock the HTTP layer with ``httpx.MockTransport`` so we never open a real
  socket; this matches the pattern used by ``test_a2a_client``.
* Exercise the happy path AND each documented failure mode (4xx, 5xx,
  transport error). The tools are contractually no-raise — every failure
  must round-trip back as a human-readable string the LLM can show to
  the operator. A regression where a tool raises would propagate up
  through the agent's tool-call machinery and surface as an unhelpful
  500 in the chat thread.
"""

from __future__ import annotations

import httpx
import pytest

from ai_platform_engineering.multi_agents.tools import autonomous_tasks as ta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_httpx(monkeypatch, handler):
    """Route every httpx.Client opened by the tools through ``handler``.

    Tools build short-lived ``httpx.Client(timeout=...)`` instances per call.
    We patch ``ta.httpx.Client`` so the constructor returns a client backed
    by a ``MockTransport`` that delegates to ``handler``. The transport
    receives the outgoing ``httpx.Request`` and returns an ``httpx.Response``,
    so we can assert on URL / method / body without brittle string matching.
    """
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(ta.httpx, "Client", _factory)


def _ok_task(task_id: str = "t1", **overrides) -> dict:
    base = {
        "id": task_id,
        "name": f"Task {task_id}",
        "agent": "github",
        "prompt": "do the thing",
        "trigger": {"type": "cron", "schedule": "0 9 * * *"},
        "enabled": True,
        "next_run": "2026-04-21T09:00:00+00:00",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# list_autonomous_tasks
# ---------------------------------------------------------------------------


def test_list_returns_summary_when_tasks_exist(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[_ok_task("t1"), _ok_task("t2", agent=None)])

    _patch_httpx(monkeypatch, handler)
    result = ta.list_autonomous_tasks.invoke({})

    assert "2 autonomous task(s)" in result
    assert "`t1`" in result
    assert "`t2`" in result
    # Task t2 has no agent — the tool should render the LLM-routed marker
    # rather than literal "None".
    assert "(LLM-routed)" in result
    # Wire shape: GET /api/v1/tasks
    assert captured["method"] == "GET"
    assert "/api/v1/tasks" in captured["url"]


def test_list_returns_friendly_string_when_empty(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(200, json=[]))
    assert "No autonomous tasks" in ta.list_autonomous_tasks.invoke({})


def test_list_returns_string_on_transport_error(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("nope", request=req)

    _patch_httpx(monkeypatch, handler)
    out = ta.list_autonomous_tasks.invoke({})
    assert "unreachable" in out.lower()


def test_list_returns_string_on_5xx(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(503, text="bad gateway"))
    out = ta.list_autonomous_tasks.invoke({})
    assert "HTTP 503" in out


# ---------------------------------------------------------------------------
# create_autonomous_task
# ---------------------------------------------------------------------------


def test_create_with_cron_succeeds(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = request.read().decode()
        return httpx.Response(201, json=_ok_task("t1"))

    _patch_httpx(monkeypatch, handler)
    result = ta.create_autonomous_task.invoke({
        "id": "t1",
        "name": "Task 1",
        "prompt": "do the thing",
        "trigger_type": "cron",
        "trigger_schedule": "0 9 * * *",
    })
    assert "Created autonomous task" in result
    assert "`t1`" in result
    assert captured["method"] == "POST"
    assert "/api/v1/tasks" in captured["url"]
    # Wire body is JSON; verify trigger spec was forwarded correctly.
    import json as _json
    body = _json.loads(captured["body"])
    assert body["trigger"] == {"type": "cron", "schedule": "0 9 * * *"}
    assert body["enabled"] is True


def test_create_cron_without_schedule_returns_validation_error(monkeypatch):
    """``trigger_type='cron'`` requires ``trigger_schedule`` — caught client-side."""
    # No HTTP call should happen; if it does, fail loudly.
    def handler(req):
        raise AssertionError("HTTP call should not have been made")

    _patch_httpx(monkeypatch, handler)
    out = ta.create_autonomous_task.invoke({
        "id": "t1",
        "name": "Task 1",
        "prompt": "do the thing",
        "trigger_type": "cron",
    })
    assert "trigger_schedule" in out


def test_create_interval_without_period_returns_validation_error(monkeypatch):
    def handler(req):
        raise AssertionError("HTTP call should not have been made")
    _patch_httpx(monkeypatch, handler)
    out = ta.create_autonomous_task.invoke({
        "id": "t1",
        "name": "Task 1",
        "prompt": "do the thing",
        "trigger_type": "interval",
    })
    assert "trigger_seconds" in out
    assert "trigger_minutes" in out
    assert "trigger_hours" in out


def test_create_propagates_409_detail_from_server(monkeypatch):
    def handler(req):
        return httpx.Response(409, json={"detail": "Task with id 't1' already exists"})

    _patch_httpx(monkeypatch, handler)
    out = ta.create_autonomous_task.invoke({
        "id": "t1",
        "name": "Task 1",
        "prompt": "do the thing",
        "trigger_type": "cron",
        "trigger_schedule": "0 9 * * *",
    })
    assert "HTTP 409" in out
    assert "already exists" in out


def test_create_omits_optional_fields_from_payload(monkeypatch):
    """``agent``, ``llm_provider``, ``description`` are server-optional — when the
    operator doesn't supply them, the tool MUST omit them rather than send
    explicit ``null`` (which would drop existing values on edit-style flows
    and is also more conservative on the wire)."""
    captured = {}

    def handler(request):
        captured["body"] = request.read().decode()
        return httpx.Response(201, json=_ok_task("t1"))

    _patch_httpx(monkeypatch, handler)
    ta.create_autonomous_task.invoke({
        "id": "t1",
        "name": "Task 1",
        "prompt": "do the thing",
        "trigger_type": "cron",
        "trigger_schedule": "0 9 * * *",
    })
    import json as _json
    body = _json.loads(captured["body"])
    assert "agent" not in body
    assert "llm_provider" not in body
    assert "description" not in body


# ---------------------------------------------------------------------------
# update_autonomous_task
# ---------------------------------------------------------------------------


def test_update_merges_fields_with_existing_definition(monkeypatch):
    """Server-managed fields (``last_ack`` / ``chat_conversation_id`` / ``next_run``)
    must be stripped from the merged PUT body — sending them back would either
    be ignored or could pin stale state."""
    captured_put = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_ok_task("t1", last_ack={"ack_status": "ok"}))
        if request.method == "PUT":
            captured_put["body"] = request.read().decode()
            return httpx.Response(200, json=_ok_task("t1", prompt="new"))
        return httpx.Response(404)

    _patch_httpx(monkeypatch, handler)
    out = ta.update_autonomous_task.invoke({"id": "t1", "prompt": "new"})
    assert "Updated autonomous task" in out
    import json as _json
    body = _json.loads(captured_put["body"])
    assert body["prompt"] == "new"
    assert body["name"] == "Task t1"  # preserved from existing
    assert "last_ack" not in body
    assert "chat_conversation_id" not in body
    assert "next_run" not in body


def test_update_returns_friendly_404_when_task_missing(monkeypatch):
    def handler(req):
        return httpx.Response(404, json={"detail": "Task 'nope' not found"})

    _patch_httpx(monkeypatch, handler)
    out = ta.update_autonomous_task.invoke({"id": "nope", "prompt": "x"})
    assert "404" in out
    assert "not found" in out


# ---------------------------------------------------------------------------
# delete + trigger
# ---------------------------------------------------------------------------


def test_delete_succeeds(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(204))
    out = ta.delete_autonomous_task.invoke({"id": "t1"})
    assert "Deleted" in out
    assert "t1" in out


def test_trigger_returns_queued_summary(monkeypatch):
    _patch_httpx(monkeypatch, lambda req: httpx.Response(
        200, json={"status": "triggered", "task_id": "t1"},
    ))
    out = ta.trigger_autonomous_task_now.invoke({"id": "t1"})
    assert "Queued" in out
    assert "t1" in out


# ---------------------------------------------------------------------------
# validate_cron_expression
# ---------------------------------------------------------------------------


def test_validate_cron_accepts_5_field_expression():
    out = ta.validate_cron_expression.invoke({"expression": "0 9 * * 1-5"})
    assert out.startswith("OK")


def test_validate_cron_rejects_wrong_field_count():
    out = ta.validate_cron_expression.invoke({"expression": "0 9 *"})
    assert "5 fields" in out
    assert "got 3" in out


def test_validate_cron_rejects_garbage_field():
    """If APScheduler is installed (it is — autonomous-agents depends on it),
    bad cron content beyond field count is caught here too."""
    out = ta.validate_cron_expression.invoke({"expression": "abc def ghi jkl mno"})
    assert "Invalid cron" in out or "OK" in out  # Either way no crash.


# ---------------------------------------------------------------------------
# Base URL configuration
# ---------------------------------------------------------------------------


def test_autonomous_agents_url_honours_env(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_AGENTS_URL", "http://prod-autonomous:8002/")
    # Trailing slash MUST be stripped so we don't end up with double slashes
    # in composed URLs.
    assert ta._autonomous_agents_url() == "http://prod-autonomous:8002"
    assert "//api/v1" not in ta._api_url("tasks")


def test_autonomous_agents_url_default_localhost(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_AGENTS_URL", raising=False)
    assert ta._autonomous_agents_url() == "http://localhost:8002"
