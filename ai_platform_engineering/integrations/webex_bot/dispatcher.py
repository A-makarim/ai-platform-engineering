# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Dispatch incoming Webex ``messages.created`` events.

The dispatcher takes a verified event, fetches the message body, and
returns one of four verdicts:

* ``DROP_LOOPGUARD`` -- the message was authored by the bot itself.
* ``DROP_NOT_THREAD_REPLY`` -- the message has no ``parentId``, so
  it isn't a reply to anything we'd routed.
* ``DROP_NO_MAPPING`` -- ``parentId`` isn't in our thread map; the
  bot didn't post the parent so this isn't our reply.
* ``FORWARD`` -- forward as a follow-up to the autonomous-agents
  service.

Forwardable replies include a ready-to-send follow-up payload.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import httpx


logger = logging.getLogger("webex_bot")


class Verdict(str, enum.Enum):
    DROP_LOOPGUARD = "drop_loopguard"
    DROP_NOT_THREAD_REPLY = "drop_not_thread_reply"
    DROP_NO_MAPPING = "drop_no_mapping"
    FORWARD = "forward"


@dataclass(frozen=True, slots=True)
class FollowUpPayload:
    """The body the bridge will POST to ``/api/v1/hooks/<task_id>/follow-up``."""

    task_id: str
    parent_run_id: str
    user_text: str
    user_ref: str | None
    transport: str = "webex"

    def to_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "parent_run_id": self.parent_run_id,
            "user_text": self.user_text,
            "transport": self.transport,
        }
        if self.user_ref:
            body["user_ref"] = self.user_ref
        return body


@dataclass(frozen=True, slots=True)
class DispatchResult:
    verdict: Verdict
    payload: FollowUpPayload | None = None
    reason: str | None = None  # human-readable detail for logs / responses


# Lookup by Webex parent message id. Production passes a Mongo-backed
# callable; tests pass an in-memory callable with the same contract.
ThreadLookup = Callable[[str], Awaitable[Mapping[str, Any] | None]]


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def verify_webex_signature(
    *, secret: str | None, body: bytes, signature_header: str | None
) -> bool:
    """Validate Webex's ``X-Spark-Signature`` header.

    Webex signs the raw request body with HMAC-SHA1 and the secret
    configured at webhook creation, then sends the lowercase hex
    digest as ``X-Spark-Signature`` (no ``sha1=`` prefix).

    Returns True if no secret is configured (signing is opt-in on
    the Webex side -- some operators ship the bridge unsigned in
    dev). Returns False on missing / mismatched headers when a
    secret IS configured.

    Empty strings are treated like ``None`` so ``WEBEX_WEBHOOK_SECRET=``
    disables signing in local development.
    """
    if not secret:
        return True
    if not signature_header:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha1
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header.lower())


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def dispatch_message_event(
    event: Mapping[str, Any],
    *,
    bot_person_id: str,
    fetch_message: Callable[[str], Awaitable[Mapping[str, Any]]],
    lookup_thread: ThreadLookup,
) -> DispatchResult:
    """Decide what to do with a Webex ``messages.created`` event.

    ``fetch_message`` and ``lookup_thread`` are injected so the pure
    decision logic is easy to test without an HTTP or Mongo fake.
    """
    data = event.get("data") or {}
    message_id = data.get("id")
    if not message_id:
        # Webex never sends this in practice; defensive log + drop.
        return DispatchResult(
            verdict=Verdict.DROP_NOT_THREAD_REPLY,
            reason="event has no data.id",
        )

    # Cheap pre-check: if the event already tells us the author is
    # the bot, drop without paying for the fetch_message round-trip.
    event_person_id = data.get("personId")
    if event_person_id and event_person_id == bot_person_id:
        return DispatchResult(
            verdict=Verdict.DROP_LOOPGUARD,
            reason="event personId matches bot",
        )

    message = await fetch_message(message_id)

    # Authoritative loop guard: even if the event lacked personId,
    # the fetched message always has it.
    if message.get("personId") == bot_person_id:
        return DispatchResult(
            verdict=Verdict.DROP_LOOPGUARD,
            reason="message personId matches bot",
        )

    parent_id = message.get("parentId")
    if not parent_id:
        return DispatchResult(
            verdict=Verdict.DROP_NOT_THREAD_REPLY,
            reason="message has no parentId",
        )

    mapping = await lookup_thread(parent_id)
    if mapping is None:
        return DispatchResult(
            verdict=Verdict.DROP_NO_MAPPING,
            reason=f"parentId {parent_id} not in thread map",
        )

    # Treat malformed rows as missing instead of crashing the bridge.
    task_id = mapping.get("task_id")
    parent_run_id = mapping.get("run_id")
    if not task_id or not parent_run_id:
        logger.warning(
            "Thread map row for parentId=%s is missing task_id/run_id "
            "(have: %s); treating as no mapping",
            parent_id,
            sorted(mapping.keys()),
        )
        return DispatchResult(
            verdict=Verdict.DROP_NO_MAPPING,
            reason=f"thread map row for {parent_id} is malformed",
        )

    user_text = (message.get("text") or message.get("markdown") or "").strip()
    if not user_text:
        # Card-only/file-only replies have no useful text to forward.
        return DispatchResult(
            verdict=Verdict.DROP_NOT_THREAD_REPLY,
            reason="message has no text body",
        )

    return DispatchResult(
        verdict=Verdict.FORWARD,
        payload=FollowUpPayload(
            task_id=task_id,
            parent_run_id=parent_run_id,
            user_text=user_text,
            user_ref=message.get("personEmail"),
        ),
    )


# ---------------------------------------------------------------------------
# Forwarder -- HTTP call to the autonomous-agents follow-up route
# ---------------------------------------------------------------------------


async def forward_followup(
    payload: FollowUpPayload,
    *,
    autonomous_agents_url: str,
    http_client: httpx.AsyncClient,
    webhook_secret: str | None = None,
    timestamp: str | None = None,
) -> httpx.Response:
    """POST a follow-up payload to ``/api/v1/hooks/<task_id>/follow-up``.

    Signs the body with the global ``WEBHOOK_SECRET`` when configured.
    The bridge cannot use per-task ``trigger.secret`` values because it
    is not part of task creation.

    When ``timestamp`` is set, the signed body is ``f"{ts}.{body}"``
    and the timestamp is sent as ``X-Webhook-Timestamp``. This must
    match the receiver's replay window (default 300s).

    Returns the raw response so the caller can log non-2xx responses
    without crashing the bridge.
    """
    body = httpx_json_compact(payload.to_json())
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if webhook_secret:
        signed = (
            timestamp.encode("utf-8") + b"." + body if timestamp else body
        )
        digest = hmac.new(
            webhook_secret.encode("utf-8"), signed, hashlib.sha256
        ).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
        if timestamp:
            headers["X-Webhook-Timestamp"] = timestamp

    url = (
        f"{str(autonomous_agents_url).rstrip('/')}"
        f"/api/v1/hooks/{payload.task_id}/follow-up"
    )
    return await http_client.post(url, content=body, headers=headers)


def httpx_json_compact(payload: Mapping[str, Any]) -> bytes:
    """Compact, deterministic JSON encoding for HMAC signing.

    Using ``json.dumps`` with sorted keys + no whitespace makes the
    signature reproducible regardless of dict ordering on either
    side. Imported lazily to keep the module's import cost trivial.
    """
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
