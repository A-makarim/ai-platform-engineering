"""Per-source dedupe-key derivation for IMP-20 trigger instances.

Single source of truth for "what counts as the same fire?" so the
webhook router, the scheduler wrapper, and the manual-trigger route
can never disagree on dedupe semantics. Each helper returns the key
plus (where applicable) any per-source metadata the caller wants to
persist on the resulting :class:`TriggerInstance` for audit purposes.

Key formats are deliberately human-readable strings rather than
opaque hashes -- a dedupe-related production incident is much
easier to triage when ``db.autonomous_trigger_instances.find()``
shows ``cron:nightly-scan:2026-04-29T09:00:00+00:00`` instead of a
32-byte blob.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

from autonomous_agents.config import get_settings
from autonomous_agents.models import TriggerSource

logger = logging.getLogger("autonomous_agents")

__all__ = [
    "derive_webhook_dedupe_key",
    "derive_scheduled_dedupe_key",
    "derive_manual_dedupe_key",
]


def _lookup_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that survives Starlette / dict inputs.

    Starlette's ``Headers`` is already case-insensitive on ``__getitem__``
    but raises ``KeyError`` on miss; bare ``dict`` inputs (used by the
    test suite) need an explicit case-insensitive walk. Centralising
    here keeps the helpers agnostic to the caller's mapping type.
    """
    try:
        value = headers.get(name)  # type: ignore[union-attr]
    except AttributeError:
        value = None
    if value:
        return value
    target = name.lower()
    for key, val in headers.items():
        if key.lower() == target:
            return val
    return None


def derive_webhook_dedupe_key(
    task_id: str,
    headers: Mapping[str, str],
    body: bytes,
) -> tuple[str, str | None]:
    """Compute ``(dedupe_key, delivery_id)`` for an inbound webhook POST.

    The header allow-list comes from
    ``Settings.webhook_delivery_id_headers`` so operators can extend it
    for custom upstreams without code changes. Headers are searched in
    list order; the first non-empty value wins. When no allow-listed
    header is present we fall back to ``sha256(body)[:16]`` so even
    unsigned senders still dedupe within the TTL window. The ``[:16]``
    truncation is purely cosmetic -- collisions across distinct bodies
    are not a security boundary, only a dedupe boundary, and 64 bits
    is plenty for that.

    The ``delivery_id`` second element is the raw header value (or
    None when the body-hash fallback fired) -- persisted on the
    trigger row for audit, NOT used in the dedupe key beyond what's
    already encoded above.
    """
    settings = get_settings()
    for header_name in settings.webhook_delivery_id_headers:
        value = _lookup_header(headers, header_name)
        if value:
            # Stripping defends against accidental leading/trailing
            # whitespace from an upstream proxy. Same value, same key.
            cleaned = value.strip()
            if cleaned:
                return f"webhook:{task_id}:{cleaned}", cleaned

    body_hash = hashlib.sha256(body).hexdigest()[:16]
    return f"webhook:{task_id}:body-{body_hash}", None


def derive_scheduled_dedupe_key(
    task_id: str,
    source: TriggerSource,
    fire_time: datetime,
) -> str:
    """Compute the dedupe key for a cron / interval fire.

    APScheduler fires within a few milliseconds of the trigger
    boundary, so rounding ``fire_time`` to second precision before
    formatting means two replicas firing the same tick converge on
    the same key and only one insert wins. The per-source prefix
    (``cron:`` vs ``interval:``) keeps the namespaces disjoint so a
    cron-and-interval-on-the-same-second collision -- highly
    unusual but possible -- doesn't suppress one of them.

    ``fire_time`` is normalised to UTC + microsecond=0 here so callers
    don't have to remember the contract.
    """
    if source not in (TriggerSource.CRON, TriggerSource.INTERVAL):
        raise ValueError(
            f"derive_scheduled_dedupe_key called with non-scheduled "
            f"source {source!r}; use derive_webhook_dedupe_key or "
            f"derive_manual_dedupe_key instead"
        )

    if fire_time.tzinfo is None:
        fire_time = fire_time.replace(tzinfo=timezone.utc)
    else:
        fire_time = fire_time.astimezone(timezone.utc)
    fire_time = fire_time.replace(microsecond=0)

    return f"{source.value}:{task_id}:{fire_time.isoformat()}"


def derive_manual_dedupe_key(
    task_id: str,
    idempotency_key: str | None,
) -> tuple[str, str | None]:
    """Compute ``(dedupe_key, idempotency_key_used)`` for a manual fire.

    The HTTP convention for ``Idempotency-Key`` is "if present, two
    requests with the same key must collapse to one effect; if
    absent, every request is a fresh effect." We honour that here:
    when the caller supplies a key we build a deterministic dedupe
    key and persist the supplied value as the trigger's
    ``delivery_id`` for audit; when absent we mint a fresh UUID so
    every click produces a distinct row (preserving the existing
    fire-and-forget behaviour of the route).
    """
    if idempotency_key is not None:
        cleaned = idempotency_key.strip()
        if cleaned:
            return f"manual:{task_id}:{cleaned}", cleaned

    # Fresh UUID -> never collides with anything else; effectively
    # "no dedup". Using a UUID rather than e.g. the wall clock keeps
    # millisecond-apart double-clicks from accidentally deduping when
    # the operator actually wants a second run.
    return f"manual:{task_id}:{uuid.uuid4()}", None
