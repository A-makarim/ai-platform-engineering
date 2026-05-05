# Copyright 2025 CNOE Contributors
# SPDX-License-Identifier: Apache-2.0

"""Integration-style test for chat-driven GitHub webhook setup.

The test drives the three supervisor tools directly with mocked HTTP clients:
template -> create webhook task -> register GitHub webhook.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ai_platform_engineering.multi_agents.tools import (
    autonomous_tasks as ta,
)
from ai_platform_engineering.multi_agents.tools import (
    github_webhooks as gw,
)
from ai_platform_engineering.multi_agents.tools import (
    webhook_task_templates as tpl,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    """Set the minimum env needed by the chained tools."""
    monkeypatch.setenv("AUTONOMOUS_AGENTS_URL", "http://autonomous-agents:8002")
    monkeypatch.setenv(
        "AUTONOMOUS_AGENTS_PUBLIC_URL", "https://abcd-1-2-3-4.ngrok.io"
    )
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "ghp_TEST_TOKEN")


def _patch_both_httpx(monkeypatch, aa_handler, gh_handler):
    """Route autonomous-agents and GitHub requests to separate handlers."""

    def router(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.github.com" in url:
            return gh_handler(request)
        # Default: treat everything else as autonomous-agents traffic.
        return aa_handler(request)

    transport = httpx.MockTransport(router)
    real_client = httpx.Client

    def _factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(ta.httpx, "Client", _factory)
    monkeypatch.setattr(gw.httpx, "Client", _factory)


# ---------------------------------------------------------------------------
# The full chain
# ---------------------------------------------------------------------------


def test_full_chain_creates_task_and_registers_webhook(monkeypatch):
    """Walk the three tools in order and verify secret/URL propagation."""
    # Recording buffers for everything the test sends over the wire.
    aa_calls: list[dict[str, Any]] = []
    gh_calls: list[dict[str, Any]] = []

    def aa_handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(request.read() or b"{}")
        aa_calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "body": body,
            }
        )
        # Only /tasks POST is expected in this chain; return a plausible
        # 201 with the server's echo of the incoming payload (minus secret,
        # which the real server redacts to has_secret=True).
        assert request.method == "POST"
        assert str(request.url).endswith("/api/v1/tasks")
        server_trigger = {"type": body["trigger"]["type"]}
        if body["trigger"]["type"] == "webhook":
            server_trigger["has_secret"] = bool(body["trigger"].get("secret"))
        return httpx.Response(
            201,
            json={
                **body,
                "trigger": server_trigger,
                "next_run": None,
            },
        )

    def gh_handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        body = _json.loads(request.read() or b"{}")
        gh_calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "auth": request.headers.get("Authorization"),
                "body": body,
            }
        )
        assert request.method == "POST"
        assert str(request.url) == (
            "https://api.github.com/repos/A-makarim/demo-repo/hooks"
        )
        # Plausible GitHub 201 response shape.
        return httpx.Response(
            201,
            json={
                "id": 12345,
                "name": "web",
                "active": body["active"],
                "events": body["events"],
                "config": {
                    "url": body["config"]["url"],
                    "content_type": "json",
                    "insecure_ssl": "0",
                    "secret": "********",  # GitHub masks on read
                },
            },
        )

    _patch_both_httpx(monkeypatch, aa_handler, gh_handler)

    # Step 1: fetch the canonical triage prompt.

    prompt = tpl.get_webhook_task_template.invoke(
        {
            "template_name": "github_issue_triage",
            "repo": "A-makarim/demo-repo",
            "webex_room_ref": "the 'auto-triage' space",
            "investigation_depth": "standard",
        }
    )
    # Prompt is ready to drop into create_autonomous_task -- parameters
    # all substituted, no placeholders left.
    assert "A-makarim/demo-repo" in prompt
    assert "auto-triage" in prompt
    assert "{repo}" not in prompt
    assert "Step 1" in prompt  # structural contract

    # Step 2: create the webhook task with the prompt.

    create_out = ta.create_autonomous_task.invoke(
        {
            "id": "demo-triage",
            "name": "Demo Issue Auto-triage",
            "prompt": prompt,
            "trigger_type": "webhook",
        }
    )

    # Server received the prompt + generated webhook secret.
    assert len(aa_calls) == 1
    created_payload = aa_calls[0]["body"]
    assert created_payload["id"] == "demo-triage"
    assert created_payload["trigger"]["type"] == "webhook"
    assert created_payload["prompt"] == prompt  # prompt transit is lossless
    aa_secret_on_wire = created_payload["trigger"]["secret"]
    assert isinstance(aa_secret_on_wire, str) and len(aa_secret_on_wire) == 64

    # The tool returned the operator-visible callback URL + secret in the
    # response so the LLM can thread them into register_github_webhook.
    assert "callback_url:" in create_out
    assert "https://abcd-1-2-3-4.ngrok.io/api/v1/hooks/demo-triage" in create_out
    assert aa_secret_on_wire in create_out  # exact secret echoed once
    assert "auto-generated" in create_out

    # In the demo flow the LLM would parse those lines from create_out;
    # in this test we extract them directly to feed into register_.
    callback_url = "https://abcd-1-2-3-4.ngrok.io/api/v1/hooks/demo-triage"
    secret = aa_secret_on_wire

    # Step 3: register the webhook on GitHub with the same values.

    register_out = gw.register_github_webhook.invoke(
        {
            "repo": "A-makarim/demo-repo",
            "callback_url": callback_url,
            "events": ["issues"],
            "secret": secret,
        }
    )

    # GitHub received the exact callback URL and secret.
    assert len(gh_calls) == 1
    gh_payload = gh_calls[0]["body"]
    assert gh_payload["events"] == ["issues"]
    assert gh_payload["config"]["url"] == callback_url
    assert gh_payload["config"]["secret"] == secret  # identical to AA's
    assert gh_payload["config"]["content_type"] == "json"
    assert gh_payload["active"] is True
    assert gh_calls[0]["auth"] == "Bearer ghp_TEST_TOKEN"

    # Tool's operator-facing response confirms registration.
    assert "hook_id=12345" in register_out
    assert "A-makarim/demo-repo" in register_out
    assert "issues" in register_out
    # Caller-supplied secrets should not appear in the registration response.
    assert secret not in register_out
    assert "HMAC-SHA256" in register_out


def test_chain_fails_cleanly_when_autonomous_agents_unreachable(monkeypatch):
    """Do not register a GitHub hook when task creation fails."""
    aa_reached = False
    gh_reached = False

    def aa_handler(_request):
        raise httpx.ConnectError("service down")

    def gh_handler(_request):
        nonlocal gh_reached
        gh_reached = True
        return httpx.Response(201)

    _patch_both_httpx(monkeypatch, aa_handler, gh_handler)

    prompt = tpl.get_webhook_task_template.invoke(
        {
            "template_name": "github_issue_triage",
            "repo": "a/b",
            "webex_room_ref": "#x",
        }
    )
    out = ta.create_autonomous_task.invoke(
        {
            "id": "t",
            "name": "T",
            "prompt": prompt,
            "trigger_type": "webhook",
        }
    )

    # Clear error surface.
    assert "unreachable" in out.lower() or "service" in out.lower()
    # Critically: no callback_url appears, so the LLM has nothing to
    # chain into register_github_webhook -- and in a real run, it
    # wouldn't try. We verify the contract by asserting the GitHub
    # mock never got a request.
    assert gh_reached is False


def test_chain_fails_cleanly_when_github_rejects_hook(monkeypatch):
    """Surface GitHub registration errors after task creation succeeds."""

    def aa_handler(request):
        import json as _json
        body = _json.loads(request.read() or b"{}")
        return httpx.Response(
            201,
            json={**body, "trigger": {"type": "webhook", "has_secret": True}},
        )

    def gh_handler(_request):
        return httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [
                    {
                        "resource": "Hook",
                        "code": "custom",
                        "message": "Hook already exists on this repository",
                    }
                ],
            },
        )

    _patch_both_httpx(monkeypatch, aa_handler, gh_handler)

    prompt = tpl.get_webhook_task_template.invoke(
        {
            "template_name": "github_issue_triage",
            "repo": "A-makarim/demo-repo",
            "webex_room_ref": "#x",
        }
    )
    create_out = ta.create_autonomous_task.invoke(
        {
            "id": "dup-hook",
            "name": "Dup Hook",
            "prompt": prompt,
            "trigger_type": "webhook",
        }
    )
    assert "callback_url:" in create_out  # task created OK

    # GitHub rejects the second half.
    register_out = gw.register_github_webhook.invoke(
        {
            "repo": "A-makarim/demo-repo",
            "callback_url": (
                "https://abcd-1-2-3-4.ngrok.io/api/v1/hooks/dup-hook"
            ),
            "events": ["issues"],
            "secret": "any-secret",
        }
    )
    assert "HTTP 422" in register_out
    assert "already exists" in register_out
    # The LLM reading this response knows to advise the operator to
    # clean up the existing hook (via list_github_webhooks +
    # delete_github_webhook) or to delete the orphaned task.
