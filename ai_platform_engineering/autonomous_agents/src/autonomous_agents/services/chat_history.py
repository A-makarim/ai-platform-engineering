# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Chat history publisher — surfaces autonomous runs in the UI's chat history.

IMP-13. Operations folks live in the chat sidebar; autonomous task runs
that never appear there are effectively invisible. This module writes
each run as a tagged conversation (``source: "autonomous"``) into the
existing ``conversations`` + ``messages`` collections used by the
Next.js UI, so a single ``?source=autonomous`` filter shows "what did
the autonomous agent do today?" alongside human chats.

Design
------
* **Opt-in.** ``CHAT_HISTORY_PUBLISH_ENABLED`` defaults to ``False``.
  We never touch UI collections unless an operator explicitly enables
  the feature, because the UI Mongo schema is owned by another package.
* **Same Mongo cluster, optionally different database.** Defaults to
  the same ``MONGODB_URI`` / ``MONGODB_DATABASE`` used by the run
  store; ``CHAT_HISTORY_DATABASE`` overrides only the database name
  when CAIPE puts chat data on a separate logical DB.
* **Deterministic ids.** Conversation ``_id`` and message
  ``message_id`` are derived from ``run_id`` via UUID5 / suffix so the
  RUNNING -> SUCCESS upsert lands on the same documents instead of
  spawning a duplicate row per status transition.
* **Best-effort.** The publisher is observability, not the source of
  truth: a flaky chat database must never abort a scheduled task. The
  scheduler wraps every call in :func:`publish_safely` (analogous to
  ``_record_safely``) so failures are logged and swallowed.

Schema notes
------------
The ``Conversation`` and ``Message`` document shapes mirror the UI's
``ui/src/types/mongodb.ts`` interfaces. We also set three fields the
UI doesn't currently declare in TypeScript but that the GET route
already keys off (``source``) plus two we add for deep-linking
(``task_id``, ``run_id``); these are additive and ignored by older UI
builds. The companion change in ``ui/src/types/mongodb.ts`` declares
them so future UI code can consume them safely.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from autonomous_agents.models import TaskRun, TaskStatus

logger = logging.getLogger("autonomous_agents")

# Module-level UUID5 namespace for deriving conversation ids from run ids.
# Random-but-fixed bytes generated once: the value is opaque -- what matters
# is that it never changes, otherwise an old run record's conversation_id
# would no longer match the document we'd write today.
_AUTONOMOUS_NS = uuid.UUID("4b2c0d6e-5b71-4f4a-9b4d-7c1e9f0a2b8e")


def _conversation_id_for_run(run_id: str) -> str:
    """Derive a stable UUIDv4-shaped conversation id from ``run_id``.

    The UI's chat routes ``validateUUID`` the path segment, so the id
    must match the canonical 8-4-4-4-12 hex pattern. ``uuid5`` returns
    a UUID with version bits set to 5 -- the regex doesn't care about
    version bits, only shape, so this is safe.
    """
    return str(uuid.uuid5(_AUTONOMOUS_NS, run_id))


@runtime_checkable
class ChatHistoryPublisher(Protocol):
    """Async publisher contract -- one method, no return value.

    Implementations MUST be safe to call concurrently from the
    scheduler event loop. They MAY raise on transient store failures
    (e.g. ``MongoChatHistoryPublisher`` lets the underlying motor
    exception propagate so operators see the real cause); the
    scheduler always wraps the call in
    ``scheduler._publish_safely`` which catches and logs, so a
    raising implementation never aborts task execution. Implementing
    swallow-and-log inside the publisher is allowed but not required.
    """

    async def publish_run(
        self,
        run: TaskRun,
        *,
        prompt: str,
        response: str | None,
        error: str | None,
        agent: str | None,
        conversation_id: str | None = None,
    ) -> None:
        """Upsert one conversation + its two messages for ``run``."""
        ...


class NoopChatHistoryPublisher:
    """No-op implementation used when the feature is disabled.

    Returned by :func:`create_chat_history_publisher` when
    ``CHAT_HISTORY_PUBLISH_ENABLED`` is False or the Mongo settings
    needed to reach the UI's chat database are missing. Keeping the
    happy-path / disabled-path interfaces identical means the
    scheduler doesn't need any "is publishing on?" branches.
    """

    async def publish_run(
        self,
        run: TaskRun,
        *,
        prompt: str,
        response: str | None,
        error: str | None,
        agent: str | None,
        conversation_id: str | None = None,
    ) -> None:
        return None


class MongoChatHistoryPublisher:
    """Writes autonomous runs into the UI's ``conversations`` + ``messages`` collections.

    The constructor takes an already-built motor client so callers
    own its lifecycle (and tests can inject ``AsyncMongoMockClient``
    from ``mongomock_motor``).

    Idempotency
    -----------
    * Conversation ``_id`` is ``uuid5(_AUTONOMOUS_NS, run_id)`` --
      stable across the RUNNING -> SUCCESS|FAILED transition.
    * Message documents are upserted by ``message_id`` derived as
      ``f"{run_id}-{role}"``. We don't pin Mongo's ``_id`` to that
      string because the UI's ``Message._id`` interface declares
      ``ObjectId``; using a string ``_id`` would round-trip oddly
      when the UI later serialises the doc.
    """

    def __init__(
        self,
        client: Any,
        database_name: str,
        *,
        owner_email: str,
        conversations_collection: str = "conversations",
        messages_collection: str = "messages",
    ) -> None:
        if not database_name:
            raise ValueError("database_name must be a non-empty string")
        if not owner_email:
            raise ValueError("owner_email must be a non-empty string")
        if not conversations_collection:
            raise ValueError("conversations_collection must be a non-empty string")
        if not messages_collection:
            raise ValueError("messages_collection must be a non-empty string")
        self._client = client
        self._db = client[database_name]
        self._conversations = self._db[conversations_collection]
        self._messages = self._db[messages_collection]
        self._owner_email = owner_email

    async def ensure_indexes(self) -> None:
        """Create the indexes our queries depend on. Idempotent.

        We deliberately do NOT touch the UI's primary indexes (those
        are owned by ``ui/src/lib/mongodb.ts``); we only add the two
        that back the autonomous-only filter chip and the run -> chat
        deep link. Mongo's ``create_index`` is a no-op when an
        identical spec already exists.
        """
        # Filter chip: ``GET /api/chat/conversations?source=autonomous``
        # sorts by ``updated_at desc`` (matches the existing list query).
        await self._conversations.create_index(
            [("source", 1), ("updated_at", -1)],
        )
        # Deep link: ``run_id`` is unique per run so we can always
        # find the conversation for a given autonomous run record.
        # Sparse so existing human conversations don't pay the index
        # cost (they have no ``run_id`` field).
        await self._conversations.create_index(
            [("run_id", 1)],
            unique=True,
            sparse=True,
        )
        # Message lookup follows the UI's upsert key shape:
        # ``(conversation_id, message_id)`` (see the UI's
        # ``/api/chat/conversations/[id]/messages/route.ts``). We do
        # NOT enforce global uniqueness on ``message_id`` alone --
        # the same client-generated message id may legitimately appear
        # in different conversations, and a unique index would either
        # fail to build on existing data or break the UI's normal
        # writes once enabled. Compound + non-unique gives us the
        # query coverage we need without those failure modes.
        await self._messages.create_index(
            [("conversation_id", 1), ("message_id", 1)],
        )

    async def publish_run(
        self,
        run: TaskRun,
        *,
        prompt: str,
        response: str | None,
        error: str | None,
        agent: str | None,
        conversation_id: str | None = None,
    ) -> None:
        # Allow callers to pre-compute the deterministic id (the
        # scheduler does this so it can stash conversation_id on the
        # TaskRun before we even open the Mongo connection). Fall
        # back to deriving it here so the publisher is also usable
        # standalone in tests.
        conv_id = conversation_id or _conversation_id_for_run(run.run_id)
        now = datetime.now(timezone.utc)

        # ----- Conversation document -----
        # ``$set`` keeps the published title/agent fresh on retries
        # (e.g. operator renamed the task between RUNNING and SUCCESS)
        # while ``$setOnInsert`` pins the immutable bits so re-running
        # publish on the same run never spawns a duplicate.
        await self._conversations.update_one(
            {"_id": conv_id},
            {
                "$set": {
                    "title": self._title_for(run),
                    "agent_id": agent,
                    "updated_at": now,
                    "metadata": {
                        # Mirror the UI's POST shape so the GET handler
                        # doesn't choke on a missing key. ``model_used``
                        # is a UI-only display field; ``autonomous`` is a
                        # truthful default for autonomous runs.
                        "agent_version": "autonomous-agents",
                        "model_used": "autonomous",
                        # Each run produces exactly two messages (user
                        # prompt + assistant response/error). Keeping
                        # this accurate matters for the UI's "X messages"
                        # subtitle.
                        "total_messages": 2,
                    },
                },
                "$setOnInsert": {
                    "_id": conv_id,
                    "owner_id": self._owner_email,
                    "created_at": now,
                    "sharing": {
                        "is_public": False,
                        "shared_with": [],
                        "shared_with_teams": [],
                        "share_link_enabled": False,
                    },
                    # ``tags`` doubles as a coarse search affordance --
                    # users can already filter by tag in the UI search.
                    "tags": ["autonomous", run.task_id],
                    "is_archived": False,
                    "is_pinned": False,
                    # Fields the UI's GET handler keys off but that
                    # aren't in the TypeScript Conversation interface
                    # *yet*; companion PR adds them. Existing UI builds
                    # ignore unknown fields.
                    "source": "autonomous",
                    "task_id": run.task_id,
                    "run_id": run.run_id,
                },
            },
            upsert=True,
        )

        # ----- Two messages: user prompt + assistant response/error -----
        # Order matters only for the on-screen rendering, which sorts
        # by ``created_at`` -- offset the assistant message by a few
        # microseconds so it always renders second even if the same
        # wall-clock millisecond is sampled twice.
        await self._upsert_message(
            conversation_id=conv_id,
            role="user",
            content=prompt,
            created_at=now,
            run=run,
            is_final=True,
        )

        assistant_text, is_final = self._assistant_payload(run, response, error)
        await self._upsert_message(
            conversation_id=conv_id,
            role="assistant",
            content=assistant_text,
            # +1 microsecond -- enough to disambiguate sort order
            # without being visible in the UI's HH:MM:SS rendering.
            created_at=now.replace(microsecond=now.microsecond + 1)
            if now.microsecond < 999_999
            else now,
            run=run,
            is_final=is_final,
        )

    async def _upsert_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        created_at: datetime,
        run: TaskRun,
        is_final: bool,
    ) -> None:
        message_id = f"{run.run_id}-{role}"
        # turn_id mirrors the UI convention -- one turn per (user,
        # assistant) pair. Using ``run_id`` makes both messages share
        # a turn so the UI's debug panel groups them correctly.
        turn_id = f"autonomous-{run.run_id}"
        # Filter by ``(conversation_id, message_id)`` to mirror the
        # UI's POST upsert shape. Filtering on ``message_id`` alone
        # would risk hitting a row from a different conversation if
        # the run-id collision space ever changes.
        await self._messages.update_one(
            {"conversation_id": conversation_id, "message_id": message_id},
            {
                "$set": {
                    # ``content`` and ``is_final`` flip across the
                    # RUNNING -> SUCCESS|FAILED transition (e.g. the
                    # placeholder turns into the final response). We
                    # also bump ``updated_at`` so operators can spot
                    # the last publish attempt.
                    "role": role,
                    "content": content,
                    "updated_at": created_at,
                    "metadata": {
                        "turn_id": turn_id,
                        # Marks the row as autonomous-origin so analytics
                        # queries can pivot on metadata.source the same
                        # way the existing /api/chat/messages POST does.
                        "source": "autonomous",
                        "agent_name": run.task_name,
                        "is_final": is_final,
                        "task_id": run.task_id,
                    },
                },
                "$setOnInsert": {
                    # Pin immutables on first insert so a re-publish
                    # never overwrites the original ``created_at`` --
                    # the UI sorts the thread by ``created_at`` and
                    # would otherwise reorder rows on every retry.
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "owner_id": self._owner_email,
                    "created_at": created_at,
                },
            },
            upsert=True,
        )

    @staticmethod
    def _title_for(run: TaskRun) -> str:
        # Short, recognisable prefix lets operators visually pick out
        # autonomous rows in the sidebar even before the filter chip
        # is applied. Including the task name (not id) matches the
        # rest of the autonomous UI.
        return f"[Autonomous] {run.task_name}"

    @staticmethod
    def _assistant_payload(
        run: TaskRun,
        response: str | None,
        error: str | None,
    ) -> tuple[str, bool]:
        """Pick what the assistant message should say.

        Three cases:
        * SUCCESS with response text -- show the response.
        * FAILED with error -- show the error so operators don't have
          to cross-reference the run history page.
        * RUNNING (intermediate publish) -- show a placeholder and
          mark ``is_final=False`` so the UI can render a spinner.
        """
        if run.status == TaskStatus.SUCCESS and response is not None:
            return response, True
        if run.status == TaskStatus.FAILED:
            return f"Run failed: {error or 'unknown error'}", True
        # RUNNING / SKIPPED / PENDING -- transient state. The
        # scheduler currently only calls publish_run on the terminal
        # transition, but supporting intermediate publish keeps the
        # contract honest if that ever changes.
        return "Autonomous task running...", False


def create_chat_history_publisher(
    *,
    enabled: bool,
    mongodb_uri: str | None,
    chat_database: str | None,
    fallback_database: str | None,
    owner_email: str,
    conversations_collection: str = "conversations",
    messages_collection: str = "messages",
) -> ChatHistoryPublisher:
    """Build the appropriate publisher for the current configuration.

    Returns a :class:`MongoChatHistoryPublisher` when **all** of:

    * ``enabled`` is True,
    * ``mongodb_uri`` is non-empty, and
    * a database name resolves (``chat_database`` overrides; otherwise
      we fall back to ``fallback_database`` so an operator who's
      already set ``MONGODB_DATABASE`` for the run store doesn't have
      to re-state it).

    Anything else returns the no-op publisher. The motor client is
    constructed lazily inside this function but does no network I/O
    until the first ``publish_run`` call.
    """
    if not enabled:
        return NoopChatHistoryPublisher()

    database = chat_database or fallback_database
    if not mongodb_uri or not database:
        # Loud warning rather than silent fallback: an operator who
        # set ``CHAT_HISTORY_PUBLISH_ENABLED=true`` clearly *wants*
        # publishing on, so a misconfig should be obvious in the
        # startup log instead of looking like the feature works but
        # produces no data.
        logger.warning(
            "Chat history publishing enabled but Mongo not configured "
            "(mongodb_uri=%s, chat_database=%s, fallback_database=%s) -- "
            "no-op publisher in use; autonomous runs will NOT show up "
            "in the chat sidebar.",
            bool(mongodb_uri),
            chat_database,
            fallback_database,
        )
        return NoopChatHistoryPublisher()

    # Same lazy-import pattern as ``run_store.create_run_store`` so
    # callers that only need the no-op publisher don't pay motor's
    # import cost.
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(
        mongodb_uri,
        tz_aware=True,
        tzinfo=timezone.utc,
    )
    return MongoChatHistoryPublisher(
        client,
        database,
        owner_email=owner_email,
        conversations_collection=conversations_collection,
        messages_collection=messages_collection,
    )
