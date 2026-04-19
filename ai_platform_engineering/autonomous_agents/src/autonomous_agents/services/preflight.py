"""Pre-flight acknowledgement against the supervisor (spec #099, FR-001..005).

When the operator creates or edits an autonomous task, we ask the supervisor
to **describe what it would do** — without actually running anything. The
supervisor returns a structured ``Acknowledgement`` payload that we persist
on the ``TaskDefinition`` as ``last_ack``. Operators see the result as a
badge in the UI ("Ack OK", "Ack failed: agent disabled", "Ack pending"),
and as the first message in the task's chat thread.

Why this is its own module rather than living in ``a2a_client``:

* It does NOT use the circuit breaker. The breaker exists to protect the
  scheduler from fan-out against a sick supervisor — a single human-driven
  preflight call has neither fan-out nor the same urgency profile, and
  contributing to breaker pressure here would noise up real run failures.

* It uses a **short** per-call timeout (10s default, overridable). A
  preflight that hangs for 5 minutes defeats the whole point — the user
  is sitting in the form waiting for the badge to update.

* It expects a specific response shape (``preflight_ack`` artifact with a
  structured DataPart) that the run-time path doesn't care about. Keeping
  the parser local avoids polluting ``a2a_client.invoke_agent``.

The payload contract MUST stay in lockstep with ``_build_preflight_ack``
in the supervisor's ``agent_executor_single.py``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field

from autonomous_agents.config import get_settings

logger = logging.getLogger("autonomous_agents")

__all__ = ["Acknowledgement", "preflight"]


# Default per-call timeout in seconds. Deliberately tight — a slow
# supervisor here means we surface "ack pending; supervisor slow" in the
# UI rather than wedging the form. Overridable via ``timeout_seconds``
# kwarg on ``preflight()``.
PREFLIGHT_TIMEOUT_SECONDS_DEFAULT = 10.0


class Acknowledgement(BaseModel):
    """Structured pre-flight result from the supervisor.

    Mirrors the payload built by the supervisor's ``_build_preflight_ack``
    plus a couple of client-side fields (``ack_status`` semantics extended
    to cover transport-level failures the supervisor never sees).
    """

    ack_status: Literal["ok", "warn", "failed", "pending"] = Field(
        ...,
        description=(
            "ok = supervisor confirmed the routing path is viable. "
            "warn = supervisor reachable but flagged a soft issue (e.g. "
            "MAS still loading, slow upstream). failed = supervisor "
            "reachable and flagged a hard issue (e.g. unknown agent). "
            "pending = supervisor unreachable; will retry."
        ),
    )
    ack_detail: str = Field(
        default="",
        description="Human-readable detail line shown in the UI badge tooltip.",
    )
    routed_to: Optional[str] = Field(
        default=None,
        description="Sub-agent the prompt would route to, or null if no hint.",
    )
    tools: list[str] = Field(
        default_factory=list,
        description="Tool names the routed sub-agent has loaded.",
    )
    available_agents: list[str] = Field(
        default_factory=list,
        description="All sub-agents currently loaded in the supervisor.",
    )
    credentials_status: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of credential-name -> status (e.g. 'github_pat': 'ok'). "
            "Empty in the light preflight; populated by future heavy-probe."
        ),
    )
    dry_run_summary: str = Field(
        default="",
        description="Plain-English summary of what the task will do at run time.",
    )
    ack_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When this ack was produced (server time).",
    )

    @classmethod
    def transport_failure(cls, detail: str) -> "Acknowledgement":
        """Build an ack representing 'supervisor never answered'."""
        return cls(
            ack_status="pending",
            ack_detail=detail,
            routed_to=None,
            tools=[],
            available_agents=[],
            credentials_status={},
            dry_run_summary="Supervisor unreachable; will retry on next task touch.",
        )

    @classmethod
    def application_failure(cls, detail: str) -> "Acknowledgement":
        """Build an ack representing 'supervisor answered with a JSON-RPC error'."""
        return cls(
            ack_status="failed",
            ack_detail=detail,
            routed_to=None,
            tools=[],
            available_agents=[],
            credentials_status={},
            dry_run_summary="Supervisor refused the preflight; see ack_detail.",
        )


def _extract_ack_payload(response_json: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Pull the supervisor's preflight DataPart out of an A2A response.

    The supervisor emits exactly one artifact named ``preflight_ack`` whose
    ``parts[0]`` is a DataPart (``kind=='data'``) holding the dict. Earlier
    a2a-sdk versions also surface ``data`` directly; both shapes are
    tolerated to avoid a hard SDK pin in this module.
    """
    result = response_json.get("result") or {}
    for artifact in result.get("artifacts", []) or []:
        if artifact.get("name") != "preflight_ack":
            continue
        for part in artifact.get("parts", []) or []:
            # New SDK: {"kind": "data", "data": {...}}
            if isinstance(part, dict):
                if part.get("kind") == "data" and isinstance(part.get("data"), dict):
                    return part["data"]
                # Some intermediaries unwrap to {"root": {"data": {...}}}
                root = part.get("root")
                if isinstance(root, dict) and isinstance(root.get("data"), dict):
                    return root["data"]
    return None


async def preflight(
    *,
    task_id: str,
    prompt: str,
    agent: Optional[str] = None,
    llm_provider: Optional[str] = None,
    timeout_seconds: float = PREFLIGHT_TIMEOUT_SECONDS_DEFAULT,
) -> Acknowledgement:
    """Send a pre-flight ``message/send`` to the supervisor and parse the ack.

    NEVER raises on failure — every failure mode is mapped to an
    :class:`Acknowledgement` with an appropriate ``ack_status``. The
    caller persists the ack regardless of status; the UI surfaces it.

    Args:
        task_id: Stable task identifier (used to derive the deterministic
            UUIDv5 contextId so this preflight shares conversation state
            with the actual scheduled runs).
        prompt: The task's prompt text. Sent as-is so the supervisor can
            display it back in the dry-run summary.
        agent: Optional routing hint (sub-agent id). When set, the
            supervisor checks whether this sub-agent is loaded.
        llm_provider: Optional LLM provider name. Echoed in the summary.
        timeout_seconds: Per-call HTTP timeout. Default tight (10s) so
            a slow supervisor doesn't wedge the form.
    """
    settings = get_settings()
    context_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"autonomous-task:{task_id}"))
    message_id = str(uuid.uuid4())

    metadata: dict[str, Any] = {"preflight": True}
    if agent:
        metadata["agent"] = agent
    effective_llm = llm_provider or settings.llm_provider
    if effective_llm:
        metadata["llm_provider"] = effective_llm

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                # Supervisor echoes this prompt in the dry_run_summary; it
                # is NOT executed. We intentionally skip the in-band
                # routing directive used by ``invoke_agent`` since the
                # preflight code path doesn't run the LLM router.
                "parts": [{"kind": "text", "text": prompt or ""}],
                "messageId": message_id,
                "contextId": context_uuid,
                "metadata": metadata,
            },
            "configuration": {
                "blocking": True,
                "acceptedOutputModes": ["text"],
            },
        },
    }

    logger.info(
        "preflight: task=%s agent=%s llm=%s timeout=%.1fs",
        task_id, agent, effective_llm, timeout_seconds,
    )

    # No tenacity, no circuit breaker — preflight is a one-shot.
    # If it fails the user gets an ack_status='pending' badge and can
    # re-trigger via "Re-ack" in the UI; we don't want to bury that
    # behind 30s of silent retries.
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(settings.supervisor_url, json=payload)
            resp.raise_for_status()
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("preflight: transport failure for task=%s: %s", task_id, exc)
        return Acknowledgement.transport_failure(
            f"Supervisor at {settings.supervisor_url} did not respond: {exc}"
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "preflight: HTTP %s from supervisor for task=%s",
            exc.response.status_code, task_id,
        )
        return Acknowledgement.application_failure(
            f"Supervisor returned HTTP {exc.response.status_code} on preflight."
        )

    try:
        body = resp.json()
    except ValueError as exc:
        logger.warning("preflight: non-JSON response for task=%s: %s", task_id, exc)
        return Acknowledgement.application_failure("Supervisor returned non-JSON response.")

    if "error" in body:
        return Acknowledgement.application_failure(
            f"Supervisor JSON-RPC error: {body['error']}"
        )

    ack_dict = _extract_ack_payload(body)
    if ack_dict is None:
        # Supervisor returned a normal response (not a preflight artifact).
        # Most likely the supervisor is on a build that doesn't yet
        # implement preflight — treat as a soft failure so the UI shows
        # "Ack pending" rather than a hard "failed" badge.
        logger.warning(
            "preflight: no preflight_ack artifact in response for task=%s "
            "(supervisor build may predate spec #099)", task_id,
        )
        return Acknowledgement.transport_failure(
            "Supervisor responded but did not return a preflight_ack artifact "
            "(supervisor may need to be upgraded to a build that supports preflight)."
        )

    try:
        return Acknowledgement(**ack_dict)
    except Exception as exc:
        # Defensive: contract drift between supervisor and client. We
        # surface a helpful detail rather than crash the form.
        logger.warning(
            "preflight: payload validation failed for task=%s: %s", task_id, exc,
        )
        return Acknowledgement.application_failure(
            f"Supervisor returned an unrecognised preflight payload: {exc}"
        )
