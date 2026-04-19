# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the scheduler <-> ChatHistoryPublisher wiring (IMP-13).

These tests assert the public effect of running ``execute_task``:

* On SUCCESS / FAILED, the configured publisher receives one
  ``publish_run`` call carrying the prompt, response/error, and
  agent.
* The pre-computed ``conversation_id`` lands on the persisted
  ``TaskRun`` so the UI can deep-link to the chat from a run row.
* A flaky publisher must not abort the task or affect the
  authoritative run-history record.

The A2A side (``invoke_agent``) is mocked so there's no network
dependency on a live supervisor.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from autonomous_agents.models import CronTrigger, TaskDefinition, TaskStatus
from autonomous_agents.scheduler import (
    execute_task,
    set_chat_history_publisher,
    set_persistence_service,
)
from autonomous_agents.services.chat_history import _conversation_id_for_run
from autonomous_agents.services.mongo import MongoDBService


class _RecordingPublisher:
    """Captures every publish_run invocation for later assertions.

    Implements the ``ChatHistoryPublisher`` protocol structurally so
    ``isinstance(_, ChatHistoryPublisher)`` passes without inheritance.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def publish_run(
        self,
        run,
        *,
        prompt,
        response,
        error,
        agent,
        conversation_id=None,
    ) -> None:
        self.calls.append(
            {
                "run_id": run.run_id,
                "task_id": run.task_id,
                "status": run.status,
                "prompt": prompt,
                "response": response,
                "error": error,
                "agent": agent,
                "conversation_id": conversation_id,
            }
        )


class _FlakyPublisher:
    """Raises on every publish_run -- simulates a chat-DB outage.

    Counts invocations so tests can assert the scheduler still
    *attempted* to publish (i.e. the wiring is sound) even when the
    publisher itself blew up.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def publish_run(self, run, **kwargs) -> None:
        self.calls += 1
        raise RuntimeError("simulated chat-history outage")


@pytest.fixture(autouse=True)
def _reset_scheduler_globals():
    """Restore both module-level singletons after every test."""
    import autonomous_agents.scheduler as sched_mod

    original_run = sched_mod._mongo_service
    original_pub = sched_mod._chat_history_publisher
    sched_mod._mongo_service = None
    sched_mod._chat_history_publisher = None
    yield
    sched_mod._mongo_service = original_run
    sched_mod._chat_history_publisher = original_pub


@pytest.fixture
def store() -> MongoDBService:
    s = MongoDBService(AsyncMongoMockClient(), database_name="test_db")
    set_persistence_service(s)
    return s


@pytest.fixture
def publisher() -> _RecordingPublisher:
    p = _RecordingPublisher()
    set_chat_history_publisher(p)
    return p


@pytest.fixture
def task() -> TaskDefinition:
    return TaskDefinition(
        id="weekly-prs",
        name="Weekly PR Review",
        agent="github",
        prompt="list open PRs",
        trigger=CronTrigger(schedule="0 9 * * MON"),
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_successful_run_is_published_with_response(
    store: MongoDBService,
    publisher: _RecordingPublisher,
    task: TaskDefinition,
):
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="here are the PRs"),
    ):
        run = await execute_task(task)

    assert run.status == TaskStatus.SUCCESS
    assert len(publisher.calls) == 1
    call = publisher.calls[0]
    assert call["run_id"] == run.run_id
    assert call["status"] == TaskStatus.SUCCESS
    assert call["prompt"] == "list open PRs"
    assert call["response"] == "here are the PRs"
    assert call["error"] is None
    assert call["agent"] == "github"
    assert call["conversation_id"] == run.conversation_id


async def test_failed_run_is_published_with_error(
    store: MongoDBService,
    publisher: _RecordingPublisher,
    task: TaskDefinition,
):
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(side_effect=RuntimeError("supervisor down")),
    ):
        run = await execute_task(task)

    assert run.status == TaskStatus.FAILED
    assert len(publisher.calls) == 1
    call = publisher.calls[0]
    assert call["status"] == TaskStatus.FAILED
    assert call["response"] is None
    assert call["error"] == "supervisor down"


async def test_conversation_id_is_set_on_taskrun_and_matches_derivation(
    store: MongoDBService,
    publisher: _RecordingPublisher,
    task: TaskDefinition,
):
    """The UI deep-links from a run row to ``/chat/<conversation_id>``;
    that means the run record itself MUST carry the same id the
    publisher wrote -- otherwise the link 404s."""
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="ok"),
    ):
        run = await execute_task(task)

    assert run.conversation_id is not None
    assert run.conversation_id == _conversation_id_for_run(run.run_id)
    persisted = (await store.list_runs())[0]
    assert persisted.conversation_id == run.conversation_id


async def test_webhook_context_is_redacted_in_published_prompt_by_default(
    store: MongoDBService,
    publisher: _RecordingPublisher,
    task: TaskDefinition,
):
    """By default, webhook payloads must NOT be inlined into the
    published prompt. The chat-history rows are read-accessible to
    all authenticated UI users (operator/audit visibility), so
    surfacing raw webhook contents would be a data-exposure
    regression (PR #10 Codex P1 review). Operators who explicitly
    opt in via ``CHAT_HISTORY_INCLUDE_CONTEXT=true`` get the
    inlined payload back -- see the companion test below."""
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="ok"),
    ):
        await execute_task(task, context={"event": "pull_request.opened", "pr": 42})

    assert len(publisher.calls) == 1
    prompt = publisher.calls[0]["prompt"]
    assert prompt.startswith("list open PRs")
    # Marker is present so operators can still see "context fired".
    assert "Context: <redacted" in prompt
    # Raw payload is NOT inlined.
    assert "pull_request.opened" not in prompt
    assert "42" not in prompt


async def test_webhook_context_is_inlined_when_opted_in(
    store: MongoDBService,
    publisher: _RecordingPublisher,
    task: TaskDefinition,
    monkeypatch,
):
    """``CHAT_HISTORY_INCLUDE_CONTEXT=true`` brings back the
    pre-redaction behavior for operators who have decided the data
    in their webhook payloads is safe to surface broadly."""
    from autonomous_agents.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_HISTORY_INCLUDE_CONTEXT", "true")
    try:
        with patch(
            "autonomous_agents.scheduler.invoke_agent",
            new=AsyncMock(return_value="ok"),
        ):
            await execute_task(
                task,
                context={"event": "pull_request.opened", "pr": 42},
            )

        prompt = publisher.calls[0]["prompt"]
        assert prompt.startswith("list open PRs")
        assert "Context:" in prompt
        assert "pull_request.opened" in prompt
        assert "42" in prompt
    finally:
        get_settings.cache_clear()


async def test_unserialisable_context_does_not_abort_task(
    store: MongoDBService,
    publisher: _RecordingPublisher,
    task: TaskDefinition,
    monkeypatch,
):
    """A non-JSON-serialisable webhook payload must not bubble out
    of execute_task -- prompt construction lives inside
    ``_publish_safely``'s try/except (PR #10 Copilot review)."""
    from autonomous_agents.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("CHAT_HISTORY_INCLUDE_CONTEXT", "true")
    try:
        # ``object()`` is a deliberate JSON-hostile sentinel.
        weird_context = {"sentinel": object()}
        with patch(
            "autonomous_agents.scheduler.invoke_agent",
            new=AsyncMock(return_value="ok"),
        ):
            run = await execute_task(task, context=weird_context)
        # The task must still complete normally.
        assert run.status == TaskStatus.SUCCESS
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


async def test_publisher_failure_does_not_abort_task(
    store: MongoDBService,
    task: TaskDefinition,
):
    """A broken chat-history publisher must never bubble out of
    execute_task -- it's observability, not source-of-truth."""
    flaky = _FlakyPublisher()
    set_chat_history_publisher(flaky)

    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="ok"),
    ):
        run = await execute_task(task)

    assert run.status == TaskStatus.SUCCESS
    assert run.response_preview == "ok"
    assert flaky.calls == 1
    # RunStore record must still hold the terminal state -- the
    # authoritative history is unaffected by the chat-publisher
    # failure.
    persisted = (await store.list_runs())[0]
    assert persisted.status == TaskStatus.SUCCESS


async def test_publisher_failure_is_logged_at_error_level(
    store: MongoDBService,
    task: TaskDefinition,
    caplog,
):
    """Operators must still see chat-publishing failures -- silent
    swallow would hide a misconfigured Mongo/permissions issue."""
    set_chat_history_publisher(_FlakyPublisher())

    with caplog.at_level("ERROR", logger="autonomous_agents"):
        with patch(
            "autonomous_agents.scheduler.invoke_agent",
            new=AsyncMock(return_value="ok"),
        ):
            await execute_task(task)

    error_messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
    assert any("Failed to publish run" in msg for msg in error_messages)


async def test_default_publisher_is_noop_when_unset(
    store: MongoDBService,
    task: TaskDefinition,
):
    """If the lifespan hook never injected a publisher, scheduler
    functions must still work -- they fall back to a no-op publisher."""
    # Note: _reset_scheduler_globals nulled the publisher; do NOT
    # set one here.
    with patch(
        "autonomous_agents.scheduler.invoke_agent",
        new=AsyncMock(return_value="ok"),
    ):
        run = await execute_task(task)
    assert run.status == TaskStatus.SUCCESS
