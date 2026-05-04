# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Read-only Mongo accessor for ``webex_thread_map``.

The autonomous-agents service writes this collection on every task
run that posts a Webex message (see
``services/mongo.py::record_webex_thread``). The bridge only needs
lookup-by-id, so the store exposes one method.

Schema (pinned ``_id``):
    {
      "_id":        "<webex-message-id>",   # parent message
      "message_id": "<webex-message-id>",   # mirrors _id
      "task_id":    "<task-uuid>",
      "run_id":     "<run-uuid>",
      "room_id":    "<webex-room-id>",      # optional
      "created_at": ISODate("..."),         # TTL'd by autonomous-agents
    }
"""

from __future__ import annotations

from typing import Any, Mapping


class WebexThreadStore:
    """Async Mongo lookup for a Webex parent-message id."""

    def __init__(self, collection: Any) -> None:
        # Accept either a Motor collection or an in-memory test fake.
        self._collection = collection

    async def lookup(self, message_id: str) -> Mapping[str, Any] | None:
        """Return the thread-map row for ``message_id`` or ``None``."""
        if not message_id:
            return None
        return await self._collection.find_one({"_id": message_id})
