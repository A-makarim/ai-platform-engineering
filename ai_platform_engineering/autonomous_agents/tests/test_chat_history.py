# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the chat-history publisher (IMP-13).

Uses ``mongomock_motor.AsyncMongoMockClient`` so the suite needs no
real MongoDB instance. Covers:

* deterministic conversation id derivation
* upsert idempotency across RUNNING -> SUCCESS|FAILED transitions
* feature-disabled (no-op) path
* misconfigured-but-enabled path (still no-op, with warning)
* schema invariants the UI's GET handler depends on
  (``source: "autonomous"``, valid UUID ``_id``, ``owner_id`` set)
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

from autonomous_agents.models import TaskRun, TaskStatus
from autonomous_agents.services.chat_history import (
    ChatHistoryPublisher,
    MongoChatHistoryPublisher,
    NoopChatHistoryPublisher,
    _conversation_id_for_run,
    create_chat_history_publisher,
)

# Regex from ``ui/src/lib/api-middleware.ts``::validateUUID -- kept
# locally so a UI-side change forces a deliberate update here too.
_UI_UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _make_run(
    run_id: str = "run-001",
    task_id: str = "t1",
    task_name: str = "Task One",
    status: TaskStatus = TaskStatus.SUCCESS,
    response_preview: str | None = "ok",
    error: str | None = None,
) -> TaskRun:
    return TaskRun(
        run_id=run_id,
        task_id=task_id,
        task_name=task_name,
        status=status,
        started_at=datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 4, 18, 10, 0, 1, tzinfo=timezone.utc),
        response_preview=response_preview,
        error=error,
    )


@pytest.fixture
def publisher() -> MongoChatHistoryPublisher:
    """Fresh in-memory Mongo + publisher per test for isolation."""
    client = AsyncMongoMockClient()
    return MongoChatHistoryPublisher(
        client,
        database_name="caipe_test",
        owner_email="autonomous@system",
    )


# ---------------------------------------------------------------------------
# Deterministic id derivation
# ---------------------------------------------------------------------------


def test_conversation_id_is_deterministic_for_same_run_id():
    a = _conversation_id_for_run("run-abc")
    b = _conversation_id_for_run("run-abc")
    assert a == b


def test_conversation_id_differs_across_runs():
    a = _conversation_id_for_run("run-abc")
    b = _conversation_id_for_run("run-xyz")
    assert a != b


def test_conversation_id_matches_ui_uuid_regex():
    """The UI's chat routes ``validateUUID`` the path segment -- if our
    derived id doesn't match the canonical 8-4-4-4-12 hex shape, every
    deep-link from a run row would 400 before hitting Mongo."""
    cid = _conversation_id_for_run(str(uuid.uuid4()))
    assert _UI_UUID_REGEX.match(cid), f"derived id {cid!r} fails UI UUID regex"


# ---------------------------------------------------------------------------
# Constructor guards
# ---------------------------------------------------------------------------


def test_constructor_rejects_empty_database_name():
    client = AsyncMongoMockClient()
    with pytest.raises(ValueError):
        MongoChatHistoryPublisher(
            client,
            database_name="",
            owner_email="a@b",
        )


def test_constructor_rejects_empty_owner_email():
    client = AsyncMongoMockClient()
    with pytest.raises(ValueError):
        MongoChatHistoryPublisher(
            client,
            database_name="db",
            owner_email="",
        )


def test_constructor_rejects_empty_collection_names():
    client = AsyncMongoMockClient()
    with pytest.raises(ValueError):
        MongoChatHistoryPublisher(
            client,
            database_name="db",
            owner_email="a@b",
            conversations_collection="",
        )
    with pytest.raises(ValueError):
        MongoChatHistoryPublisher(
            client,
            database_name="db",
            owner_email="a@b",
            messages_collection="",
        )


def test_publisher_satisfies_protocol():
    client = AsyncMongoMockClient()
    assert isinstance(
        MongoChatHistoryPublisher(client, database_name="db", owner_email="a@b"),
        ChatHistoryPublisher,
    )
    assert isinstance(NoopChatHistoryPublisher(), ChatHistoryPublisher)


# ---------------------------------------------------------------------------
# Happy-path schema
# ---------------------------------------------------------------------------


async def test_publish_run_writes_one_conversation_and_two_messages(
    publisher: MongoChatHistoryPublisher,
):
    run = _make_run()
    await publisher.publish_run(
        run,
        prompt="do the thing",
        response="thing done",
        error=None,
        agent="github",
    )

    convs = [doc async for doc in publisher._conversations.find({})]
    msgs = [doc async for doc in publisher._messages.find({})]
    assert len(convs) == 1
    assert len(msgs) == 2


async def test_conversation_document_carries_required_ui_fields(
    publisher: MongoChatHistoryPublisher,
):
    """The UI's GET /api/chat/conversations relies on these fields --
    losing any one of them silently breaks the autonomous filter chip
    or the conversation render."""
    run = _make_run(run_id="r1", task_id="weekly-prs", task_name="Weekly PR Review")
    await publisher.publish_run(
        run,
        prompt="list open prs",
        response="here they are",
        error=None,
        agent="github",
    )

    conv = await publisher._conversations.find_one({})
    # Must be a valid UUID-shape so deep links pass validateUUID.
    assert _UI_UUID_REGEX.match(conv["_id"])
    assert conv["source"] == "autonomous"
    assert conv["owner_id"] == "autonomous@system"
    assert conv["agent_id"] == "github"
    assert conv["task_id"] == "weekly-prs"
    assert conv["run_id"] == "r1"
    assert "autonomous" in conv["tags"]
    assert "weekly-prs" in conv["tags"]
    assert conv["is_archived"] is False
    assert conv["is_pinned"] is False
    # Mirror the UI's POST-shape so its render code finds total_messages.
    assert conv["metadata"]["total_messages"] == 2


async def test_message_documents_carry_user_then_assistant_with_source_tag(
    publisher: MongoChatHistoryPublisher,
):
    run = _make_run()
    await publisher.publish_run(
        run,
        prompt="hello",
        response="world",
        error=None,
        agent="github",
    )

    msgs = [doc async for doc in publisher._messages.find({}).sort("created_at", 1)]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["content"] == "world"
    for m in msgs:
        assert m["metadata"]["source"] == "autonomous"
        assert m["metadata"]["task_id"] == run.task_id
        # Same turn so the UI's debug panel groups them.
        assert m["metadata"]["turn_id"] == f"autonomous-{run.run_id}"
    # Assistant marked final on terminal SUCCESS so the UI doesn't
    # show a perpetual spinner.
    assert msgs[1]["metadata"]["is_final"] is True


# ---------------------------------------------------------------------------
# Failure rendering
# ---------------------------------------------------------------------------


async def test_failed_run_assistant_message_carries_error_text(
    publisher: MongoChatHistoryPublisher,
):
    run = _make_run(status=TaskStatus.FAILED, response_preview=None, error="boom")
    await publisher.publish_run(
        run,
        prompt="please work",
        response=None,
        error="boom",
        agent="github",
    )

    assistant = await publisher._messages.find_one({"role": "assistant"})
    assert "boom" in assistant["content"]
    assert assistant["metadata"]["is_final"] is True


async def test_failed_run_with_no_error_string_uses_unknown_placeholder(
    publisher: MongoChatHistoryPublisher,
):
    run = _make_run(status=TaskStatus.FAILED, response_preview=None, error=None)
    await publisher.publish_run(
        run,
        prompt="please work",
        response=None,
        error=None,
        agent=None,
    )

    assistant = await publisher._messages.find_one({"role": "assistant"})
    assert "unknown error" in assistant["content"]


# ---------------------------------------------------------------------------
# Idempotency (RUNNING -> SUCCESS shouldn't dupe)
# ---------------------------------------------------------------------------


async def test_publish_run_is_idempotent_across_status_transitions(
    publisher: MongoChatHistoryPublisher,
):
    """The scheduler today only publishes on terminal state, but the
    contract is "publishing twice doesn't dupe". A future refactor
    that adds an intermediate publish must not silently spawn extra
    rows in the chat sidebar."""
    run = _make_run(status=TaskStatus.RUNNING, response_preview=None, error=None)
    await publisher.publish_run(
        run,
        prompt="hello",
        response=None,
        error=None,
        agent="github",
    )

    # Same run_id, terminal state.
    run.status = TaskStatus.SUCCESS
    run.response_preview = "world"
    await publisher.publish_run(
        run,
        prompt="hello",
        response="world",
        error=None,
        agent="github",
    )

    convs = [doc async for doc in publisher._conversations.find({})]
    msgs = [doc async for doc in publisher._messages.find({})]
    assert len(convs) == 1
    assert len(msgs) == 2
    # Final assistant message reflects the SUCCESS payload, not the
    # RUNNING placeholder.
    assistant = await publisher._messages.find_one({"role": "assistant"})
    assert assistant["content"] == "world"
    assert assistant["metadata"]["is_final"] is True


async def test_re_publish_preserves_original_created_at_and_bumps_updated_at(
    publisher: MongoChatHistoryPublisher,
):
    """When a run transitions RUNNING -> SUCCESS the publisher
    re-upserts both messages. The UI sorts the thread by
    ``created_at``, so overwriting it would reorder rows on every
    retry (PR #10 Copilot review). ``created_at`` is pinned in
    ``$setOnInsert``; ``updated_at`` tracks the latest publish.
    """
    run = _make_run(status=TaskStatus.RUNNING, response_preview=None, error=None)
    await publisher.publish_run(
        run, prompt="hello", response=None, error=None, agent="github"
    )
    first = await publisher._messages.find_one({"role": "assistant"})
    original_created_at = first["created_at"]

    # Same run id, terminal state, fresh wall clock.
    run.status = TaskStatus.SUCCESS
    run.response_preview = "world"
    await publisher.publish_run(
        run, prompt="hello", response="world", error=None, agent="github"
    )
    second = await publisher._messages.find_one({"role": "assistant"})

    assert second["created_at"] == original_created_at
    # Content reflects the terminal state; sort key did NOT shift.
    assert second["content"] == "world"
    # ``updated_at`` is set on every publish so operators can spot
    # the latest attempt without losing the original timestamp.
    assert "updated_at" in second
    assert second["updated_at"] >= original_created_at


async def test_message_upsert_filter_includes_conversation_id(
    publisher: MongoChatHistoryPublisher,
):
    """Two runs that *somehow* generated the same ``message_id``
    suffix (different run-id namespaces, schema migration, etc.)
    must NOT cross-write into each other. The filter is keyed on
    ``(conversation_id, message_id)`` -- PR #10 Copilot review."""
    run_a = _make_run(run_id="ra")
    run_b = _make_run(run_id="rb")

    await publisher.publish_run(
        run_a, prompt="prompt-a", response="resp-a", error=None, agent="github"
    )
    await publisher.publish_run(
        run_b, prompt="prompt-b", response="resp-b", error=None, agent="github"
    )

    # Two distinct conversations, two distinct message pairs.
    convs = [doc async for doc in publisher._conversations.find({})]
    msgs = [doc async for doc in publisher._messages.find({})]
    assert len(convs) == 2
    assert len(msgs) == 4
    # No row was overwritten across conversations.
    contents = sorted(m["content"] for m in msgs)
    assert contents == ["prompt-a", "prompt-b", "resp-a", "resp-b"]


async def test_publish_uses_explicit_conversation_id_when_provided(
    publisher: MongoChatHistoryPublisher,
):
    """The scheduler pre-computes the conversation id so it can stash
    it on the TaskRun before persisting -- the publisher must honour
    that id rather than re-derive (otherwise a future change to the
    derivation would orphan the run record)."""
    run = _make_run(run_id="r-zzz")
    explicit = "11111111-1111-1111-1111-111111111111"
    await publisher.publish_run(
        run,
        prompt="hi",
        response="hello",
        error=None,
        agent="github",
        conversation_id=explicit,
    )

    conv = await publisher._conversations.find_one({})
    assert conv["_id"] == explicit


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


async def test_ensure_indexes_creates_filter_and_deeplink_indexes(
    publisher: MongoChatHistoryPublisher,
):
    await publisher.ensure_indexes()
    info = await publisher._conversations.index_information()
    keys = {tuple(idx["key"]) for idx in info.values()}
    # Filter chip: source + recency.
    assert (("source", 1), ("updated_at", -1)) in keys
    # Deep link from run row -> chat.
    assert (("run_id", 1),) in keys

    msg_info = await publisher._messages.index_information()
    msg_keys = {tuple(idx["key"]) for idx in msg_info.values()}
    # Compound (conversation_id, message_id) -- mirrors the UI's
    # message upsert key shape (see PR #10 Copilot review). NOT a
    # unique index on ``message_id`` alone, because the same
    # client-generated message id may legitimately appear in
    # different conversations.
    assert (("conversation_id", 1), ("message_id", 1)) in msg_keys


async def test_ensure_indexes_is_idempotent(publisher: MongoChatHistoryPublisher):
    await publisher.ensure_indexes()
    await publisher.ensure_indexes()


# ---------------------------------------------------------------------------
# No-op publisher
# ---------------------------------------------------------------------------


async def test_noop_publisher_returns_none_and_writes_nothing():
    pub = NoopChatHistoryPublisher()
    result = await pub.publish_run(
        _make_run(),
        prompt="x",
        response="y",
        error=None,
        agent="github",
    )
    assert result is None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def test_create_publisher_returns_noop_when_disabled():
    pub = create_chat_history_publisher(
        enabled=False,
        mongodb_uri="mongodb://localhost",
        chat_database="caipe",
        fallback_database=None,
        owner_email="autonomous@system",
    )
    assert isinstance(pub, NoopChatHistoryPublisher)


def test_create_publisher_returns_noop_when_uri_missing(caplog):
    with caplog.at_level(logging.WARNING, logger="autonomous_agents"):
        pub = create_chat_history_publisher(
            enabled=True,
            mongodb_uri=None,
            chat_database="caipe",
            fallback_database=None,
            owner_email="autonomous@system",
        )
    assert isinstance(pub, NoopChatHistoryPublisher)
    assert any("Chat history publishing enabled" in r.message for r in caplog.records)


def test_create_publisher_returns_noop_when_database_missing(caplog):
    with caplog.at_level(logging.WARNING, logger="autonomous_agents"):
        pub = create_chat_history_publisher(
            enabled=True,
            mongodb_uri="mongodb://localhost",
            chat_database=None,
            fallback_database=None,
            owner_email="autonomous@system",
        )
    assert isinstance(pub, NoopChatHistoryPublisher)


def test_create_publisher_falls_back_to_run_store_database():
    """An operator who already set ``MONGODB_DATABASE`` for the run
    store shouldn't have to re-state it for chat publishing."""
    pub = create_chat_history_publisher(
        enabled=True,
        mongodb_uri="mongodb://localhost",
        chat_database=None,
        fallback_database="caipe",
        owner_email="autonomous@system",
    )
    assert isinstance(pub, MongoChatHistoryPublisher)
    assert pub._db.name == "caipe"


def test_create_publisher_chat_database_overrides_fallback():
    pub = create_chat_history_publisher(
        enabled=True,
        mongodb_uri="mongodb://localhost",
        chat_database="caipe_chat",
        fallback_database="caipe_runs",
        owner_email="autonomous@system",
    )
    assert isinstance(pub, MongoChatHistoryPublisher)
    assert pub._db.name == "caipe_chat"
