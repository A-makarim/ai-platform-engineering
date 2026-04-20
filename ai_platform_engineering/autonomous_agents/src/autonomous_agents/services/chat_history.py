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
from typing import Any, Literal, Protocol, runtime_checkable

from autonomous_agents.models import TaskDefinition, TaskRun, TaskStatus

logger = logging.getLogger("autonomous_agents")

# Module-level UUID5 namespace for deriving conversation ids.
#
# Random-but-fixed bytes generated once: the value is opaque -- what matters
# is that it never changes, otherwise an old run record's conversation_id
# would no longer match the document we'd write today.
_AUTONOMOUS_NS = uuid.UUID("4b2c0d6e-5b71-4f4a-9b4d-7c1e9f0a2b8e")


# Spec #099 FR-007 — enumerated message kinds. Each chat message we write
# carries one of these in ``metadata.kind`` so the UI can render a typed
# affordance (status icon, contextual menu) without re-parsing the body.
# Kept open ("str") so future kinds can land without a co-deploy of the UI.
MessageKind = Literal[
    "creation_intent",   # initial human-authored intent + form values
    "preflight_ack",     # supervisor's pre-flight acknowledgement payload
    "next_run_marker",   # informational: "next run at HH:MM" — Phase 2
    "run_request",       # the prompt actually sent to the supervisor
    "run_response",      # successful supervisor response
    "run_error",         # failed supervisor response (with error detail)
    "task_updated",      # task fields changed (post-Phase-1)
    "task_disabled",     # operator disabled the task (post-Phase-1)
    "task_deleted",      # operator deleted the task (post-Phase-1)
    "task_reauthored",   # task author bot rewrote the task (Phase 3)
]


def _conversation_id_for_task(task_id: str) -> str:
    """Derive a stable UUIDv4-shaped conversation id from ``task_id``.

    Spec #099 FR-006 / AD-002. Every autonomous task owns exactly one chat
    conversation. The deterministic UUIDv5 derivation matches the contextId
    derivation used by ``services/a2a_client.py`` so the chat conversation,
    the LangGraph checkpointer thread, and the supervisor's run history
    all key off the same identifier.

    The UI's chat routes ``validateUUID`` the path segment, so the id
    must match the canonical 8-4-4-4-12 hex pattern. ``uuid5`` returns
    a UUID with version bits set to 5 — the regex doesn't care about
    version bits, only shape, so this is safe.
    """
    return str(uuid.uuid5(_AUTONOMOUS_NS, f"task:{task_id}"))


# Backwards-compatible alias for callers that still pass run_id (e.g.
# integration tests built against the per-run scheme). Per spec #099 the
# canonical derivation is per-task; this alias deliberately maps to the
# same NEW per-task namespace when possible by stripping the run_id and
# extracting the embedded task — but since run_ids are pure UUIDs with
# no embedded task hint, the safest fallback is to derive a per-run id
# under the OLD scheme so any test harness still in use stays green.
def _conversation_id_for_run(run_id: str) -> str:
    """Deprecated: per-run conversation id. Prefer ``_conversation_id_for_task``."""
    return str(uuid.uuid5(_AUTONOMOUS_NS, run_id))


@runtime_checkable
class ChatHistoryPublisher(Protocol):
    """Async publisher contract — one conversation per task, many messages.

    Spec #099 FR-006..009: a task owns a single conversation (deterministic
    UUIDv5 from the task id) and every lifecycle event for that task
    appends a typed message (``metadata.kind``) to the same thread.

    Implementations MUST be safe to call concurrently from the scheduler
    and from the FastAPI request handlers. They MAY raise on transient
    store failures; callers wrap every call in ``scheduler._publish_safely``
    or ``routes.tasks._publish_safely`` (forthcoming) so a raising
    implementation never aborts task execution or 500s a CRUD route.
    """

    async def publish_run(
        self,
        run: TaskRun,
        *,
        prompt: str,
        response: str | None,
        error: str | None,
        agent: str | None,
        task_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """Append a (run_request, run_response|run_error) message pair."""
        ...

    async def publish_creation_intent(
        self,
        task: TaskDefinition,
    ) -> None:
        """Append a creation_intent message describing the operator's request."""
        ...

    async def publish_preflight_ack(
        self,
        task: TaskDefinition,
        ack_payload: dict[str, Any],
    ) -> None:
        """Append a preflight_ack message with the supervisor's ack payload."""
        ...


class NoopChatHistoryPublisher:
    """No-op implementation used when the feature is disabled.

    Returned by :func:`create_chat_history_publisher` when
    ``CHAT_HISTORY_PUBLISH_ENABLED`` is False or the Mongo settings
    needed to reach the UI's chat database are missing. Keeping the
    happy-path / disabled-path interfaces identical means the callers
    don't need any "is publishing on?" branches.
    """

    async def publish_run(
        self,
        run: TaskRun,
        *,
        prompt: str,
        response: str | None,
        error: str | None,
        agent: str | None,
        task_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        return None

    async def publish_creation_intent(
        self,
        task: TaskDefinition,
    ) -> None:
        return None

    async def publish_preflight_ack(
        self,
        task: TaskDefinition,
        ack_payload: dict[str, Any],
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

    # ------------------------------------------------------------------
    # Spec #099 — per-task conversation publishers
    # ------------------------------------------------------------------

    async def publish_creation_intent(
        self,
        task: TaskDefinition,
    ) -> None:
        """Append (or upsert if first time) a ``creation_intent`` message.

        Idempotent on (conversation_id, message_id) so a re-create of an
        already-existing task (rare but possible during operator
        re-import flows) doesn't duplicate the intent line.
        """
        conv_id = _conversation_id_for_task(task.id)
        now = datetime.now(timezone.utc)
        await self._upsert_conversation(conv_id, task=task, now=now)

        body_lines = [
            f"Created task '{task.name}' (id: {task.id}).",
            f"Target sub-agent: {task.agent or '(LLM router will choose)'}",
            f"Trigger: {task.trigger.type}",
        ]
        if getattr(task.trigger, "schedule", None):
            body_lines.append(f"Schedule (cron): {task.trigger.schedule}")
        if task.llm_provider:
            body_lines.append(f"LLM provider override: {task.llm_provider}")
        body_lines.extend(["", "Prompt:", task.prompt])

        await self._upsert_kind_message(
            conversation_id=conv_id,
            message_id=f"task:{task.id}:creation_intent",
            role="user",
            kind="creation_intent",
            content="\n".join(body_lines),
            created_at=now,
            task=task,
            extra_meta={"created_via": "form"},  # Phase 3 may set "chat"
        )

    async def publish_preflight_ack(
        self,
        task: TaskDefinition,
        ack_payload: dict[str, Any],
    ) -> None:
        """Append a ``preflight_ack`` assistant message with the structured ack.

        Idempotent on a stable id derived from (task_id, ack_at) so
        successive re-acks (e.g. user edited the prompt) accumulate as
        separate messages while accidental retries collapse.
        """
        conv_id = _conversation_id_for_task(task.id)
        now = datetime.now(timezone.utc)
        await self._upsert_conversation(conv_id, task=task, now=now)

        ack_at = ack_payload.get("ack_at") or now.isoformat()
        msg_id = f"task:{task.id}:preflight_ack:{ack_at}"

        status = ack_payload.get("ack_status", "unknown")
        detail = ack_payload.get("ack_detail", "")
        summary = ack_payload.get("dry_run_summary", "")

        # Render a human-readable body alongside the structured payload.
        # The UI picks rendering by ``metadata.kind`` (Phase 2); the
        # ``content`` text is the fallback for any client that doesn't
        # know about the kind discriminator yet.
        body_lines = [
            f"Pre-flight: {status.upper()}.",
        ]
        if detail:
            body_lines.append(detail)
        if summary:
            body_lines.append("")
            body_lines.append(summary)

        await self._upsert_kind_message(
            conversation_id=conv_id,
            message_id=msg_id,
            role="assistant",
            kind="preflight_ack",
            content="\n".join(body_lines),
            created_at=now,
            task=task,
            extra_meta={"ack_payload": ack_payload},
            is_final=True,
        )

    async def publish_run(
        self,
        run: TaskRun,
        *,
        prompt: str,
        response: str | None,
        error: str | None,
        agent: str | None,
        task_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """Append (run_request, run_response|run_error) for one run.

        Spec #099 FR-007: each run accumulates as TWO new messages on the
        same per-task thread rather than overwriting the same two slots.
        Multiple runs of the same task therefore form a chronological
        history visible in the sidebar.
        """
        # The scheduler stamps TaskRun.task_id from the task definition,
        # so the ``task_id`` kwarg is just an explicit override slot for
        # callers (and tests) that want to pin it themselves.
        effective_task_id = task_id or run.task_id
        conv_id = conversation_id or _conversation_id_for_task(effective_task_id)
        now = datetime.now(timezone.utc)

        await self._upsert_conversation(
            conv_id, task_id=effective_task_id, agent=agent,
            title=f"[Autonomous] {run.task_name}", now=now,
        )

        # ----- run_request: the prompt actually sent to the supervisor -----
        await self._upsert_kind_message(
            conversation_id=conv_id,
            message_id=f"run:{run.run_id}:request",
            role="user",
            kind="run_request",
            content=prompt,
            created_at=now,
            run=run,
            extra_meta={"run_id": run.run_id, "task_id": effective_task_id},
            is_final=True,
        )

        # ----- run_response | run_error: the supervisor's reply -----
        if run.status == TaskStatus.FAILED:
            kind: MessageKind = "run_error"
            content = f"Run failed: {error or 'unknown error'}"
            is_final = True
        elif run.status == TaskStatus.SUCCESS and response is not None:
            kind = "run_response"
            content = response
            is_final = True
        else:
            kind = "run_response"
            content = "Autonomous task running..."
            is_final = False

        # +1 microsecond keeps assistant after user when both writes
        # land on the same wall-clock millisecond. Same trick the old
        # publisher used; preserved here for stable sort order.
        assistant_at = (
            now.replace(microsecond=now.microsecond + 1)
            if now.microsecond < 999_999 else now
        )
        await self._upsert_kind_message(
            conversation_id=conv_id,
            message_id=f"run:{run.run_id}:response",
            role="assistant",
            kind=kind,
            content=content,
            created_at=assistant_at,
            run=run,
            extra_meta={
                "run_id": run.run_id,
                "task_id": effective_task_id,
                "run_status": run.status.value,
            },
            is_final=is_final,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _upsert_conversation(
        self,
        conv_id: str,
        *,
        task: TaskDefinition | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        title: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Upsert the per-task conversation document.

        Accepts EITHER a full ``TaskDefinition`` (creation_intent /
        preflight_ack paths) OR explicit ``task_id`` + ``agent`` (run
        publishing path, which doesn't require us to load the full
        task object). Title falls back to the task name when ``task``
        is provided, or is taken verbatim from ``title``.
        """
        now = now or datetime.now(timezone.utc)
        effective_task_id = task.id if task else task_id
        effective_agent = task.agent if task else agent
        if effective_task_id is None:
            raise ValueError("task or task_id must be provided")

        effective_title = title or (
            f"[Autonomous] {task.name}" if task else f"[Autonomous] {effective_task_id}"
        )

        await self._conversations.update_one(
            {"_id": conv_id},
            {
                "$set": {
                    "title": effective_title,
                    "agent_id": effective_agent,
                    "updated_at": now,
                    "metadata": {
                        "agent_version": "autonomous-agents",
                        "model_used": "autonomous",
                        # Total message count is recomputed in the UI
                        # via $count when needed; we no longer pin it
                        # here because per-task threads grow over time.
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
                    "tags": ["autonomous", effective_task_id],
                    "is_archived": False,
                    "is_pinned": False,
                    "source": "autonomous",
                    "task_id": effective_task_id,
                },
            },
            upsert=True,
        )

    async def _upsert_kind_message(
        self,
        *,
        conversation_id: str,
        message_id: str,
        role: str,
        kind: MessageKind,
        content: str,
        created_at: datetime,
        task: TaskDefinition | None = None,
        run: TaskRun | None = None,
        extra_meta: dict[str, Any] | None = None,
        is_final: bool = True,
    ) -> None:
        """Upsert a single message with a typed ``metadata.kind``.

        Filter by ``(conversation_id, message_id)`` to mirror the UI's POST
        upsert shape. ``$setOnInsert`` pins ``created_at`` so a retry never
        re-orders the thread. ``$set`` keeps content/kind fresh in case of
        legitimate amendments (e.g. response text becoming available
        after an initial RUNNING placeholder write).
        """
        meta: dict[str, Any] = {
            "kind": kind,
            "source": "autonomous",
            "is_final": is_final,
        }
        if task is not None:
            meta["task_id"] = task.id
            meta["task_name"] = task.name
        if run is not None:
            meta["task_id"] = run.task_id
            meta["task_name"] = run.task_name
        if extra_meta:
            meta.update(extra_meta)

        await self._messages.update_one(
            {"conversation_id": conversation_id, "message_id": message_id},
            {
                "$set": {
                    "role": role,
                    "content": content,
                    "updated_at": created_at,
                    "metadata": meta,
                },
                "$setOnInsert": {
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "owner_id": self._owner_email,
                    "created_at": created_at,
                },
            },
            upsert=True,
        )


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
