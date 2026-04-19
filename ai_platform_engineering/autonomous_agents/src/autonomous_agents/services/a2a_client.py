"""A2A client — sends tasks to the CAIPE supervisor agent.

Transport reliability
---------------------
The supervisor is just another HTTP service: it can be restarted, fall over
behind a load balancer, or briefly hit OOM. We retry on the failure modes
that are *transient* (5xx + connection/transport errors) and never on the
ones that are caller-fault (4xx). 4xx means the request itself is bad —
auth, validation, missing route — and replaying it would only burn quota
without changing the outcome.

The retry policy is configurable via ``Settings.a2a_max_retries`` and
``Settings.a2a_timeout_seconds``, with optional per-call overrides supplied
by the scheduler (``TaskDefinition.max_retries`` / ``timeout_seconds``).
"""

import json
import logging
import uuid
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    before_sleep_log,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from autonomous_agents.config import get_settings
from autonomous_agents.services.circuit_breaker import (
    CircuitBreakerOpenError,
    get_circuit_breaker,
)

logger = logging.getLogger("autonomous_agents")

__all__ = ["invoke_agent", "CircuitBreakerOpenError"]


def _is_retryable_exception(exc: BaseException) -> bool:
    """Return True if ``exc`` represents a transient supervisor failure.

    Retryable:
        * ``httpx.TransportError`` — connection refused, DNS failure,
          read timeout, etc. The supervisor never produced a response.
        * ``httpx.HTTPStatusError`` with status code >= 500 — the
          supervisor responded but is itself unhealthy.

    Not retryable:
        * ``httpx.HTTPStatusError`` with 4xx — caller-side bug (bad
          payload, auth failure, unknown route). Retrying is wasted work.
        * Anything else — let it propagate so we don't paper over real
          bugs (validation errors, programming errors, etc.).
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


async def _post_once(
    *,
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
) -> httpx.Response:
    """Single HTTP attempt — separated so tenacity can retry it cleanly.

    The ``client`` is owned by the caller (``invoke_agent``) so that the
    same HTTP connection pool is reused across retry attempts within a
    single ``invoke_agent`` call. Otherwise every retry would pay TCP
    handshake + TLS setup for a brand-new socket, defeating httpx's
    keep-alive entirely.
    """
    response = await client.post(url, json=payload)
    # raise_for_status inside the retry boundary so 5xx triggers a retry
    # via the HTTPStatusError branch in _is_retryable_exception.
    response.raise_for_status()
    return response


async def invoke_agent(
    prompt: str,
    task_id: str,
    agent: str | None = None,
    llm_provider: str | None = None,
    context: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    max_retries: int | None = None,
) -> str:
    """Send a prompt to the CAIPE supervisor via the A2A protocol.

    Returns the agent's text response, or raises on failure.

    The A2A message format follows the Google A2A spec:
    https://google.github.io/A2A/

    Parameters
    ----------
    timeout_seconds:
        Overrides ``Settings.a2a_timeout_seconds`` for this single call.
        Useful when the scheduler knows a particular task is long-running.
    max_retries:
        Overrides ``Settings.a2a_max_retries`` for this single call. Set
        to 0 to force a single attempt with no retries.
    """
    settings = get_settings()
    message_id = str(uuid.uuid4())

    effective_timeout = timeout_seconds if timeout_seconds is not None else settings.a2a_timeout_seconds
    effective_max_retries = max_retries if max_retries is not None else settings.a2a_max_retries

    # Augment prompt with any extra context (e.g. webhook payload)
    full_prompt = prompt
    if context:
        full_prompt = f"{prompt}\n\nContext:\n{json.dumps(context, indent=2)}"

    # Build metadata for routing — pass agent name and LLM provider to supervisor
    metadata: dict[str, Any] = {}
    if agent:
        metadata["agent"] = agent
    effective_llm = llm_provider or settings.llm_provider
    if effective_llm:
        metadata["llm_provider"] = effective_llm

    message: dict[str, Any] = {
        "role": "user",
        "parts": [{"kind": "text", "text": full_prompt}],
        "messageId": message_id,
        "contextId": f"autonomous-{task_id}",
    }
    if metadata:
        message["metadata"] = metadata

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": message,
            "configuration": {
                "blocking": True,
                "acceptedOutputModes": ["text"],
            },
        },
    }

    logger.info(
        f"Invoking supervisor at {settings.supervisor_url} for task '{task_id}' "
        f"(agent={agent!r}, llm_provider={effective_llm!r}, "
        f"timeout={effective_timeout}s, max_retries={effective_max_retries})"
    )

    # tenacity stop_after_attempt counts the *initial* attempt, so total
    # attempts = 1 + max_retries.
    retrying = AsyncRetrying(
        stop=stop_after_attempt(1 + effective_max_retries),
        wait=wait_exponential_jitter(
            initial=settings.a2a_retry_backoff_initial_seconds,
            max=settings.a2a_retry_backoff_max_seconds,
        ),
        retry=retry_if_exception(_is_retryable_exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )

    # IMP-16: gate the call through the circuit breaker. If the breaker
    # is OPEN we short-circuit *before* opening a connection, which is
    # the whole point -- a broken supervisor must not see traffic from
    # every scheduled run multiplied by the retry budget. CircuitBreakerOpenError
    # propagates to the scheduler and is recorded as the run failure
    # reason, which is much more actionable than a generic timeout.
    breaker = await get_circuit_breaker()
    await breaker.before_call(settings.supervisor_url)

    # One client per invoke_agent call, reused across retries. The
    # per-attempt timeout lives on the client (so each retry honours it)
    # and the pool is torn down once the call completes.
    try:
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            async for attempt in retrying:
                with attempt:
                    response = await _post_once(
                        client=client,
                        url=settings.supervisor_url,
                        payload=payload,
                    )
    except RetryError as exc:
        # reraise=True normally surfaces the underlying exception, but keep
        # this branch defensively for older tenacity behaviour.
        underlying = exc.last_attempt.exception()
        if _is_retryable_exception(underlying):
            await breaker.record_failure(settings.supervisor_url)
        raise underlying from exc  # pragma: no cover
    except (httpx.TransportError, httpx.HTTPStatusError) as exc:
        # Retries exhausted (or first attempt with retries=0). Count one
        # failure against the breaker -- *not* one per attempt -- so a
        # request that succeeds on retry leaves the breaker at zero.
        # Only "supervisor-is-sick" failures count: 4xx is caller-fault
        # (auth/validation/missing route) and would self-DoS the breaker
        # on a misconfigured task. We piggy-back on the same retryable-
        # classification used above so the two policies stay in sync.
        if _is_retryable_exception(exc):
            await breaker.record_failure(settings.supervisor_url)
        raise exc

    # Transport call succeeded -- close the breaker if it was tripped.
    # We treat HTTP success as supervisor-is-healthy even if the JSON-RPC
    # ``error`` branch fires below, because that's an application-level
    # error, not a connectivity / availability problem.
    await breaker.record_success(settings.supervisor_url)

    result = response.json()

    if "error" in result:
        raise RuntimeError(f"A2A error from supervisor: {result['error']}")

    # Extract text from A2A response using the same 3-step fallback as
    # utils/a2a_common/a2a_remote_agent_connect.py:
    #   1. artifacts[].parts — most agents return results here
    #   2. status.message.parts — used by some agents for final replies
    #   3. history[] last agent message — fallback when neither above is populated
    try:
        task_result = result["result"]

        # 1. Artifacts
        for artifact in task_result.get("artifacts", []):
            texts = [p["text"] for p in artifact.get("parts", []) if p.get("kind") == "text" and p.get("text")]
            if texts:
                return " ".join(texts).strip()

        # 2. Status message parts
        status_parts = task_result.get("status", {}).get("message", {}).get("parts", [])
        texts = [p["text"] for p in status_parts if p.get("kind") == "text" and p.get("text")]
        if texts:
            return " ".join(texts).strip()

        # 3. History — last agent message (skip tool-status emoji lines)
        for message in reversed(task_result.get("history", [])):
            if message.get("role") != "agent":
                continue
            texts = [
                p["text"]
                for p in message.get("parts", [])
                if p.get("kind") == "text"
                and p.get("text")
                and not p["text"].startswith(("🔧", "✅"))
            ]
            if texts:
                return " ".join(texts).strip()

    except (KeyError, TypeError):
        pass

    logger.warning(f"Unexpected A2A response shape: {result}")
    raise RuntimeError(f"Could not extract text from A2A response: {result}")
