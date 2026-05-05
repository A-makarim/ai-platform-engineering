# Copyright 2025 CNOE Contributors
# SPDX-License-Identifier: Apache-2.0

"""Tests for canonical GitHub/Webex webhook task prompt templates.

The tests check structural contracts without locking every word of the prompts.
"""

from __future__ import annotations

import pytest

from ai_platform_engineering.multi_agents.tools import webhook_task_templates as tpl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(template_name: str = "github_issue_triage", **overrides) -> str:
    args = {
        "template_name": template_name,
        "repo": "A-makarim/demo-repo",
        "webex_room_ref": "the 'auto-triage' space",
        "investigation_depth": "standard",
    }
    args.update(overrides)
    return tpl.get_webhook_task_template.invoke(args)


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["github_issue_triage", "github_pr_review", "github_push_notify"]
)
def test_template_substitutes_repo_and_room(name):
    rendered = _render(template_name=name, repo="abd/demo", webex_room_ref="#ops")
    assert "abd/demo" in rendered
    # Webex room reference should appear at least once; issue_triage /
    # pr_review use it multiple times (ack + report), push_notify once.
    assert rendered.count("#ops") >= 1
    # No unresolved placeholders left behind.
    assert "{repo}" not in rendered
    assert "{webex_room_ref}" not in rendered
    assert "{preamble}" not in rendered
    assert "{investigation_depth}" not in rendered


def test_issue_triage_has_ordered_steps():
    """Issue triage keeps its expected ordered step shape."""
    rendered = _render(template_name="github_issue_triage")
    # Explicit step headers in order. If we rename them later this
    # test changes with intent.
    for idx, marker in enumerate(["Step 1", "Step 2", "Step 3", "Step 4"], start=1):
        assert marker in rendered, f"missing {marker}"
    # Steps appear in order (Step 2's position > Step 1's, etc).
    positions = [rendered.index(f"Step {i}") for i in range(1, 5)]
    assert positions == sorted(positions)


def test_issue_triage_tells_task_to_read_payload_fields():
    """The prompt tells the task to read GitHub payload fields."""
    rendered = _render(template_name="github_issue_triage")
    assert "issue.number" in rendered
    assert "issue.title" in rendered
    assert "issue.html_url" in rendered


def test_issue_triage_signs_comment_as_auto_triage():
    """The GitHub comment should identify itself as automated."""
    rendered = _render(template_name="github_issue_triage")
    assert "CAIPE auto-triage" in rendered


def test_pr_review_does_not_approve_or_reject():
    """PR review template must leave approval decisions to humans."""
    rendered = _render(template_name="github_pr_review")
    assert "DO NOT approve or reject" in rendered


def test_pr_review_flags_secrets():
    """PR review should include secret or credential checks."""
    rendered = _render(template_name="github_pr_review")
    low = rendered.lower()
    assert "secret" in low or "credential" in low


def test_push_notify_is_compact_and_single_step():
    """Push notifications should stay compact."""
    rendered = _render(template_name="github_push_notify")
    # No multi-step numbered structure.
    assert "Step 2" not in rendered
    # References the field names the task will parse.
    assert "commits" in rendered
    assert "pusher" in rendered.lower() or "sender" in rendered.lower()


def test_investigation_depth_flows_into_prompt():
    for depth in ("shallow", "standard", "deep"):
        rendered = _render(investigation_depth=depth)
        assert f'"{depth}"' in rendered or depth in rendered


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_unknown_template_name_returns_error_string():
    result = tpl.get_webhook_task_template.invoke(
        {
            "template_name": "github_issue_triage",  # set valid then override after
            "repo": "a/b",
            "webex_room_ref": "#x",
        }
    )
    assert "Step 1" in result  # sanity: baseline valid call works

    # Unknown name -> error. Use .invoke so pydantic validation lets the
    # string pass (the tool itself handles validation).
    from ai_platform_engineering.multi_agents.tools.webhook_task_templates import (
        get_webhook_task_template,
    )
    # Bypass the Literal type hint by using .func directly -- same code
    # path the LLM would take if it hallucinated a template name.
    result = get_webhook_task_template.func(
        template_name="nonexistent",
        repo="a/b",
        webex_room_ref="#x",
    )
    assert "Unknown template" in result
    assert "github_issue_triage" in result  # lists available options


def test_blank_repo_returns_error_string():
    from ai_platform_engineering.multi_agents.tools.webhook_task_templates import (
        get_webhook_task_template,
    )
    result = get_webhook_task_template.func(
        template_name="github_issue_triage",
        repo="",
        webex_room_ref="#x",
    )
    assert "'repo'" in result
    assert "non-empty" in result


def test_blank_webex_room_ref_returns_error_string():
    from ai_platform_engineering.multi_agents.tools.webhook_task_templates import (
        get_webhook_task_template,
    )
    result = get_webhook_task_template.func(
        template_name="github_issue_triage",
        repo="a/b",
        webex_room_ref="",
    )
    assert "'webex_room_ref'" in result
    assert "non-empty" in result


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------


def test_tool_metadata_is_present():
    """Name, description, and schema metadata must be populated."""
    assert tpl.get_webhook_task_template.name == "get_webhook_task_template"
    assert len(tpl.get_webhook_task_template.description) > 100
    assert "github_issue_triage" in tpl.get_webhook_task_template.description
