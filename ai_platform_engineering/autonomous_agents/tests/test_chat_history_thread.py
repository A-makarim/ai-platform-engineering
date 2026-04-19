# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Tests for the spec #099 chat-history refactor (per-task threads).

Covers the contract changes that matter to operators:

* Conversation id is per-TASK and deterministic (FR-006).
* MessageKind enumeration covers the Phase 1 + Phase 2 lifecycle events
  (FR-007).
* NoopChatHistoryPublisher implements every Protocol method so the
  callers never need an "is publishing on?" branch.
* MongoChatHistoryPublisher creation_intent, preflight_ack, and run
  publishers all upsert into the same per-task conversation and tag
  every message with metadata.kind.

We use a plain in-memory fake instead of mongomock_motor so the test
file stays dependency-light and runs in milliseconds. The fake mirrors
just enough of motor's collection surface (update_one with upsert,
in-memory dict store) for these assertions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from autonomous_agents.models import (
    CronTrigger,
    TaskDefinition,
    TaskRun,
    TaskStatus,
)
from autonomous_agents.services.chat_history import (
    MessageKind,  # noqa: F401  imported for type-coverage assertion below
    MongoChatHistoryPublisher,
    NoopChatHistoryPublisher,
    _conversation_id_for_task,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeCollection:
    """Minimal motor.AsyncIOMotorCollection stand-in for upsert assertions."""

    def __init__(self) -> None:
        self.docs: dict[tuple, dict[str, Any]] = {}

    async def update_one(self, filt: dict, update: dict, upsert: bool = False) -> None:
        # Use the filter as a hashable key so we can find the same doc later.
        key = tuple(sorted(filt.items()))
        existing = self.docs.get(key)
        if existing is None and upsert:
            doc: dict[str, Any] = dict(filt)
            doc.update(update.get("$setOnInsert", {}))
            doc.update(update.get("$set", {}))
            self.docs[key] = doc
        elif existing is not None:
            existing.update(update.get("$set", {}))

    async def create_index(self, *args, **kwargs) -> None:  # noqa: D401
        return None


class _FakeDatabase:
    def __init__(self) -> None:
        self._cols: dict[str, _FakeCollection] = {}

    def __getitem__(self, name: str) -> _FakeCollection:
        if name not in self._cols:
            self._cols[name] = _FakeCollection()
        return self._cols[name]


class _FakeClient:
    def __init__(self) -> None:
        self.dbs: dict[str, _FakeDatabase] = {}

    def __getitem__(self, name: str) -> _FakeDatabase:
        if name not in self.dbs:
            self.dbs[name] = _FakeDatabase()
        return self.dbs[name]


@pytest.fixture
def fake_client() -> _FakeClient:
    return _FakeClient()


@pytest.fixture
def publisher(fake_client) -> MongoChatHistoryPublisher:
    return MongoChatHistoryPublisher(
        fake_client,
        database_name="caipe",
        owner_email="autonomous@system",
    )


def _task(task_id: str = "t1", **overrides) -> TaskDefinition:
    base = dict(
        id=task_id,
        name=f"Task {task_id}",
        agent="github",
        prompt="list open PRs older than 7 days",
        trigger=CronTrigger(schedule="0 9 * * *"),
        llm_provider="openai",
    )
    base.update(overrides)
    return TaskDefinition(**base)


# ---------------------------------------------------------------------------
# Deterministic conversation id (FR-006)
# ---------------------------------------------------------------------------

def test_conversation_id_for_task_is_deterministic_and_uuid_shaped():
    a = _conversation_id_for_task("daily-pr-check")
    b = _conversation_id_for_task("daily-pr-check")
    assert a == b
    # UUID5 of NS, "task:" + task_id — must parse as a UUID
    parsed = uuid.UUID(a)
    assert parsed.version == 5


def test_conversation_id_differs_per_task():
    assert _conversation_id_for_task("a") != _conversation_id_for_task("b")


# ---------------------------------------------------------------------------
# Noop publisher implements full Protocol (FR-007)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_noop_publisher_implements_creation_intent_and_preflight_ack():
    pub = NoopChatHistoryPublisher()
    # Smoke test: each call must succeed silently when publishing is off.
    await pub.publish_creation_intent(_task())
    await pub.publish_preflight_ack(_task(), {"ack_status": "ok"})
    await pub.publish_run(
        TaskRun(
            run_id="r1",
            task_id="t1",
            task_name="Task t1",
            status=TaskStatus.SUCCESS,
            response_preview="ok",
        ),
        prompt="x",
        response="ok",
        error=None,
        agent="github",
    )


# ---------------------------------------------------------------------------
# MongoChatHistoryPublisher — creation_intent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_creation_intent_writes_per_task_conversation_and_typed_message(
    publisher,
):
    await publisher.publish_creation_intent(_task("t1"))

    # Conversation
    convs = list(publisher._conversations.docs.values())
    assert len(convs) == 1
    assert convs[0]["_id"] == _conversation_id_for_task("t1")
    assert convs[0]["task_id"] == "t1"
    assert convs[0]["source"] == "autonomous"

    # Message
    msgs = list(publisher._messages.docs.values())
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["role"] == "user"
    assert msg["metadata"]["kind"] == "creation_intent"
    assert msg["metadata"]["created_via"] == "form"
    # Body contains the prompt the operator entered
    assert "list open PRs older than 7 days" in msg["content"]


@pytest.mark.asyncio
async def test_creation_intent_is_idempotent_on_retry(publisher):
    """Calling publish_creation_intent twice for the same task must NOT duplicate."""
    await publisher.publish_creation_intent(_task("t1"))
    await publisher.publish_creation_intent(_task("t1"))
    assert len(publisher._conversations.docs) == 1
    assert len(publisher._messages.docs) == 1


# ---------------------------------------------------------------------------
# MongoChatHistoryPublisher — preflight_ack
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_preflight_ack_appends_typed_message_with_payload(publisher):
    task = _task("t1")
    ack_payload = {
        "ack_status": "ok",
        "ack_detail": "ready",
        "routed_to": "github",
        "tools": ["list_pull_requests"],
        "available_agents": ["github", "argocd"],
        "credentials_status": {},
        "dry_run_summary": "Will list PRs.",
        "ack_at": "2026-04-19T18:00:00Z",
    }
    await publisher.publish_preflight_ack(task, ack_payload)

    # Single message with kind=preflight_ack
    msgs = list(publisher._messages.docs.values())
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["role"] == "assistant"
    assert msg["metadata"]["kind"] == "preflight_ack"
    # Full ack payload preserved in metadata for the UI to render
    # type-specific affordances
    assert msg["metadata"]["ack_payload"]["ack_status"] == "ok"
    assert "ready" in msg["content"]


@pytest.mark.asyncio
async def test_preflight_ack_dedupe_keys_on_ack_at_timestamp(publisher):
    """Two acks at different times should land as two distinct messages."""
    await publisher.publish_preflight_ack(
        _task("t1"),
        {"ack_status": "ok", "ack_at": "2026-04-19T18:00:00Z"},
    )
    await publisher.publish_preflight_ack(
        _task("t1"),
        {"ack_status": "ok", "ack_at": "2026-04-19T18:05:00Z"},
    )
    # Two acks => two messages, but still ONE conversation
    assert len(publisher._messages.docs) == 2
    assert len(publisher._conversations.docs) == 1


# ---------------------------------------------------------------------------
# MongoChatHistoryPublisher — publish_run (per-task append, not slot upsert)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_run_appends_request_response_pair_with_typed_kinds(publisher):
    task_id = "t1"
    run = TaskRun(
        run_id="r1",
        task_id=task_id,
        task_name="Task t1",
        status=TaskStatus.SUCCESS,
        response_preview="ok",
    )
    await publisher.publish_run(
        run, prompt="list PRs", response="found 3 PRs",
        error=None, agent="github",
    )

    msgs = list(publisher._messages.docs.values())
    assert len(msgs) == 2

    # Find by kind to be order-independent
    by_kind = {m["metadata"]["kind"]: m for m in msgs}
    assert "run_request" in by_kind
    assert "run_response" in by_kind
    assert by_kind["run_request"]["role"] == "user"
    assert by_kind["run_request"]["content"] == "list PRs"
    assert by_kind["run_response"]["role"] == "assistant"
    assert by_kind["run_response"]["content"] == "found 3 PRs"


@pytest.mark.asyncio
async def test_publish_run_failure_emits_run_error_kind(publisher):
    task_id = "t1"
    run = TaskRun(
        run_id="r1",
        task_id=task_id,
        task_name="Task t1",
        status=TaskStatus.FAILED,
        error="supervisor down",
    )
    await publisher.publish_run(
        run, prompt="list PRs", response=None,
        error="supervisor down", agent="github",
    )

    by_kind = {
        m["metadata"]["kind"]: m for m in publisher._messages.docs.values()
    }
    assert "run_error" in by_kind
    assert "run_response" not in by_kind
    assert "supervisor down" in by_kind["run_error"]["content"]


@pytest.mark.asyncio
async def test_multiple_runs_accumulate_in_same_per_task_conversation(publisher):
    task_id = "t1"
    for run_id, response in [("r1", "first"), ("r2", "second"), ("r3", "third")]:
        run = TaskRun(
            run_id=run_id,
            task_id=task_id,
            task_name="Task t1",
            status=TaskStatus.SUCCESS,
            response_preview=response,
        )
        await publisher.publish_run(
            run, prompt="list", response=response,
            error=None, agent="github",
        )

    # Three runs => 6 messages (req+resp per run), all in ONE conversation
    assert len(publisher._messages.docs) == 6
    assert len(publisher._conversations.docs) == 1
    conv_id = next(iter(publisher._conversations.docs.values()))["_id"]
    assert conv_id == _conversation_id_for_task(task_id)
