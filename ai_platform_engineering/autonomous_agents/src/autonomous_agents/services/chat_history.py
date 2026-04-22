# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Chat history publisher contracts for autonomous task events.

Purpose
-------
Autonomous task runs should appear in the existing chat UI so operators
can inspect activity via a ``source=autonomous`` filter.

Scope
-----
This module is intentionally driver-free. It defines:
- typed message kinds
- deterministic conversation id helpers
- ``ChatHistoryPublisher`` protocol
- ``NoopChatHistoryPublisher`` fallback

Mongo persistence is implemented in ``services.mongo.MongoService`` and
exposed via ``MongoChatHistoryPublisherAdapter``.

Design notes
------------
- Feature is opt-in (``CHAT_HISTORY_PUBLISH_ENABLED``).
- Conversation id is deterministic per task:
  ``uuid5(_AUTONOMOUS_NS, f"task:{task_id}")``.
- Message ids are per-run (e.g. ``run:<run_id>:request``), so runs append.
- Publishing is best-effort observability; callers handle failures safely.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, Protocol, runtime_checkable

from autonomous_agents.models import TaskDefinition, TaskRun

# Module-level UUID5 namespace for deriving conversation ids.
#
# Random-but-fixed bytes generated once: the value is opaque -- what
# matters is that it never changes, otherwise an old run record's
# conversation_id would no longer match the document we'd write today.
_AUTONOMOUS_NS = uuid.UUID("4b2c0d6e-5b71-4f4a-9b4d-7c1e9f0a2b8e")


# Spec #099 FR-007 -- enumerated message kinds. Each chat message we
# write carries one of these in ``metadata.kind`` so the UI can render
# a typed affordance (status icon, contextual menu) without re-parsing
# the body. Kept open ("str") so future kinds can land without a
# co-deploy of the UI.
MessageKind = Literal[
    "creation_intent",   # initial human-authored intent + form values
    "preflight_ack",     # supervisor's pre-flight acknowledgement payload
    "next_run_marker",   # informational: "next run at HH:MM" -- Phase 2
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

    Spec #099 FR-006 / AD-002. Every autonomous task owns exactly one
    chat conversation. The deterministic UUIDv5 derivation matches the
    contextId derivation used by ``services/a2a_client.py`` so the chat
    conversation, the LangGraph checkpointer thread, and the
    supervisor's run history all key off the same identifier.

    The UI's chat routes ``validateUUID`` the path segment, so the id
    must match the canonical 8-4-4-4-12 hex pattern. ``uuid5`` returns
    a UUID with version bits set to 5 -- the regex doesn't care about
    version bits, only shape, so this is safe.
    """
    return str(uuid.uuid5(_AUTONOMOUS_NS, f"task:{task_id}"))


def _conversation_id_for_run(run_id: str) -> str:
    """Deprecated per-run conversation id; prefer :func:`_conversation_id_for_task`.

    Retained for backwards compatibility with integration harnesses
    that were built against the pre-spec-#099 per-run namespace. New
    callers MUST use :func:`_conversation_id_for_task` so the
    conversation, checkpointer, and run history all share a key.
    """
    return str(uuid.uuid5(_AUTONOMOUS_NS, run_id))


@runtime_checkable
class ChatHistoryPublisher(Protocol):
    """Async publisher contract -- one conversation per task, many messages.

    Spec #099 FR-006..009: a task owns a single conversation
    (deterministic UUIDv5 from the task id) and every lifecycle event
    for that task appends a typed message (``metadata.kind``) to the
    same thread.

    Implementations MUST be safe to call concurrently from the
    scheduler and from the FastAPI request handlers. They MAY raise on
    transient store failures; callers wrap every call in
    ``scheduler._publish_safely`` or ``routes.tasks._publish_safely``
    so a raising implementation never aborts task execution or 500s a
    CRUD route.
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
    """No-op implementation used when chat-history publishing is disabled.

    Selected by the lifespan when ``CHAT_HISTORY_PUBLISH_ENABLED`` is
    False, or when MongoDB isn't reachable and the service fell back to
    ephemeral in-memory stores. Keeping the happy-path / disabled-path
    interfaces identical means the callers don't need any "is
    publishing on?" branches.
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