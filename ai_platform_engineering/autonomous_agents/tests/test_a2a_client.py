# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``services.a2a_client.invoke_agent`` retry behaviour.

These tests exercise the *retry classification* and *attempt budget* of
``invoke_agent`` without going over the network. The httpx layer is stubbed
out via ``_post_once`` so we can deterministically replay any combination
of (success, 4xx, 5xx, transport error) and assert the policy:

    * 5xx and ``httpx.TransportError`` are retryable.
    * 4xx is **never** retryable — replaying a caller-fault request is
      wasted work.
    * Total attempts == 1 + ``max_retries`` (per-call override beats
      ``Settings.a2a_max_retries``).
    * Per-call ``timeout_seconds`` overrides ``Settings.a2a_timeout_seconds``.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from autonomous_agents.config import Settings, get_settings
from autonomous_agents.services import a2a_client
from autonomous_agents.services import circuit_breaker as cb_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(json_body: dict[str, Any], status_code: int = 200) -> httpx.Response:
    """Build a fully-formed httpx.Response with a JSON body.

    Tenacity inspects the response status code via ``response.raise_for_status()``
    inside ``_post_once``; building a real Response keeps that contract honest.
    """
    request = httpx.Request("POST", "http://supervisor.local")
    return httpx.Response(status_code, json=json_body, request=request)


def _success_body(text: str = "ok") -> dict[str, Any]:
    """Minimal A2A success response shape that ``invoke_agent`` understands."""
    return {
        "result": {
            "artifacts": [
                {"parts": [{"kind": "text", "text": text}]},
            ]
        }
    }


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    """Build an HTTPStatusError as httpx.Response.raise_for_status() would."""
    request = httpx.Request("POST", "http://supervisor.local")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"{status_code} {response.reason_phrase}",
        request=request,
        response=response,
    )


@pytest.fixture(autouse=True)
def _fast_retries():
    """Shrink retry timings so the suite stays fast.

    The production defaults (1s initial, 30s max backoff) are correct for
    real outages but make unit tests sleep for seconds. Override settings
    via the Settings cache so all retry waits are effectively instant.
    """
    get_settings.cache_clear()
    fast = Settings(
        a2a_retry_backoff_initial_seconds=0.0,
        a2a_retry_backoff_max_seconds=0.001,
        a2a_max_retries=3,
        a2a_timeout_seconds=10.0,
        # IMP-16: keep the breaker high so the existing retry tests
        # never trip it. Dedicated breaker tests build their own
        # CircuitBreaker instance with tighter thresholds.
        circuit_breaker_failure_threshold=1000,
    )
    # Drop the cached singleton so the breaker is rebuilt with these
    # (test-only) settings on the next ``get_circuit_breaker()`` call.
    # We must patch ``cb_mod.get_settings`` too, not just
    # ``a2a_client.get_settings``: the breaker singleton reads its
    # config from the binding inside ``circuit_breaker``, which is a
    # separate import. (Caught by Copilot review on PR #9.)
    cb_mod.reset_circuit_breaker()
    with (
        patch.object(a2a_client, "get_settings", return_value=fast),
        patch.object(cb_mod, "get_settings", return_value=fast),
    ):
        yield fast
    get_settings.cache_clear()
    cb_mod.reset_circuit_breaker()


# ---------------------------------------------------------------------------
# is_retryable_exception classification
# ---------------------------------------------------------------------------

def test_is_retryable_transport_error_is_retried():
    assert a2a_client._is_retryable_exception(httpx.ConnectError("boom")) is True
    assert a2a_client._is_retryable_exception(httpx.ReadTimeout("slow")) is True


def test_is_retryable_5xx_is_retried():
    for code in (500, 502, 503, 504):
        assert a2a_client._is_retryable_exception(_http_error(code)) is True, code


def test_is_retryable_4xx_is_not_retried():
    for code in (400, 401, 403, 404, 422):
        assert a2a_client._is_retryable_exception(_http_error(code)) is False, code


def test_is_retryable_other_exception_is_not_retried():
    # ValueError represents a programming error in our caller — replaying
    # it would mask the real bug.
    assert a2a_client._is_retryable_exception(ValueError("nope")) is False


# ---------------------------------------------------------------------------
# invoke_agent — happy path
# ---------------------------------------------------------------------------

async def test_happy_path_single_attempt():
    """200 on first try → returns text, _post_once called exactly once."""
    mock_post = AsyncMock(return_value=_make_response(_success_body("hello")))
    with patch.object(a2a_client, "_post_once", new=mock_post):
        result = await a2a_client.invoke_agent(prompt="hi", task_id="t1")

    assert result == "hello"
    assert mock_post.await_count == 1


# ---------------------------------------------------------------------------
# invoke_agent — retry on 5xx
# ---------------------------------------------------------------------------

async def test_retries_on_5xx_then_succeeds():
    """First call raises 502, second returns 200 → success after 2 attempts."""
    mock_post = AsyncMock(
        side_effect=[_http_error(502), _make_response(_success_body("recovered"))]
    )
    with patch.object(a2a_client, "_post_once", new=mock_post):
        result = await a2a_client.invoke_agent(prompt="hi", task_id="t1")

    assert result == "recovered"
    assert mock_post.await_count == 2


async def test_retries_on_transport_error_then_succeeds():
    """ConnectError → 200 → success after 2 attempts."""
    mock_post = AsyncMock(
        side_effect=[
            httpx.ConnectError("supervisor down"),
            _make_response(_success_body("back online")),
        ]
    )
    with patch.object(a2a_client, "_post_once", new=mock_post):
        result = await a2a_client.invoke_agent(prompt="hi", task_id="t1")

    assert result == "back online"
    assert mock_post.await_count == 2


# ---------------------------------------------------------------------------
# invoke_agent — no retry on 4xx
# ---------------------------------------------------------------------------

async def test_does_not_retry_on_4xx():
    """A 400 must surface immediately — retrying caller-fault is wasted work."""
    mock_post = AsyncMock(side_effect=_http_error(400))
    with patch.object(a2a_client, "_post_once", new=mock_post):
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await a2a_client.invoke_agent(prompt="hi", task_id="t1")

    assert exc_info.value.response.status_code == 400
    assert mock_post.await_count == 1


# ---------------------------------------------------------------------------
# invoke_agent — exhausting the retry budget
# ---------------------------------------------------------------------------

async def test_exhausts_retries_then_reraises_last_5xx():
    """max_retries=3 → 4 total attempts → final 5xx propagates."""
    mock_post = AsyncMock(side_effect=[_http_error(503)] * 10)
    with patch.object(a2a_client, "_post_once", new=mock_post):
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await a2a_client.invoke_agent(prompt="hi", task_id="t1", max_retries=3)

    assert exc_info.value.response.status_code == 503
    # 1 initial attempt + 3 retries = 4
    assert mock_post.await_count == 4


async def test_max_retries_zero_disables_retries():
    """Per-call max_retries=0 → exactly one attempt even on 5xx."""
    mock_post = AsyncMock(side_effect=_http_error(500))
    with patch.object(a2a_client, "_post_once", new=mock_post):
        with pytest.raises(httpx.HTTPStatusError):
            await a2a_client.invoke_agent(prompt="hi", task_id="t1", max_retries=0)

    assert mock_post.await_count == 1


# ---------------------------------------------------------------------------
# invoke_agent — per-call overrides
# ---------------------------------------------------------------------------

async def test_per_call_max_retries_beats_settings(_fast_retries):
    """Settings says 3 retries; per-call max_retries=1 wins."""
    assert _fast_retries.a2a_max_retries == 3  # sanity check
    mock_post = AsyncMock(side_effect=[_http_error(500)] * 5)
    with patch.object(a2a_client, "_post_once", new=mock_post):
        with pytest.raises(httpx.HTTPStatusError):
            await a2a_client.invoke_agent(prompt="hi", task_id="t1", max_retries=1)

    # 1 initial attempt + 1 retry = 2, NOT 4 (which would be the settings default)
    assert mock_post.await_count == 2


def _spy_async_client_ctor():
    """Patch ``httpx.AsyncClient`` and capture the timeout it was built with.

    The mock client supports ``async with`` and exposes a ``post`` AsyncMock
    so callers can override its behaviour per test. We deliberately mock at
    the class boundary (not at ``_post_once``) because the IMP-02 review
    asked us to verify the client is built with the right timeout *and*
    reused across retries — both of those facts live above ``_post_once``.
    """
    from unittest.mock import MagicMock

    instances: list[MagicMock] = []
    constructor_kwargs: list[dict[str, Any]] = []

    def factory(*args, **kwargs):
        constructor_kwargs.append(kwargs)
        instance = MagicMock(name="AsyncClient")
        instance.post = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instances.append(instance)
        return instance

    return factory, instances, constructor_kwargs


async def test_per_call_timeout_passed_to_async_client(_fast_retries):
    """timeout_seconds=42 reaches the underlying httpx.AsyncClient."""
    assert _fast_retries.a2a_timeout_seconds == 10.0  # sanity check
    factory, _instances, ctor_kwargs = _spy_async_client_ctor()

    def factory_with_response(*a, **kw):
        inst = factory(*a, **kw)
        inst.post = AsyncMock(return_value=_make_response(_success_body("ok")))
        return inst

    with patch.object(a2a_client.httpx, "AsyncClient", new=factory_with_response):
        await a2a_client.invoke_agent(prompt="hi", task_id="t1", timeout_seconds=42.0)

    assert ctor_kwargs[-1]["timeout"] == 42.0


async def test_settings_timeout_used_when_no_override(_fast_retries):
    """When no per-call timeout is given, the Settings default is used."""
    factory, instances, ctor_kwargs = _spy_async_client_ctor()

    def factory_with_response(*a, **kw):
        inst = factory(*a, **kw)
        inst.post = AsyncMock(return_value=_make_response(_success_body("ok")))
        return inst

    with patch.object(a2a_client.httpx, "AsyncClient", new=factory_with_response):
        await a2a_client.invoke_agent(prompt="hi", task_id="t1")

    assert ctor_kwargs[-1]["timeout"] == _fast_retries.a2a_timeout_seconds


async def test_single_async_client_reused_across_retries(_fast_retries):
    """Across multiple retry attempts in one ``invoke_agent`` call, exactly
    one ``httpx.AsyncClient`` is constructed.

    Locks in the connection-pool reuse fix from the Copilot review: an
    earlier draft created a fresh client per attempt, paying TCP+TLS
    handshake on every retry and defeating httpx keep-alive.
    """
    factory, instances, ctor_kwargs = _spy_async_client_ctor()

    def factory_with_responses(*a, **kw):
        inst = factory(*a, **kw)
        # 502 → 502 → 200 forces 3 attempts on a single client.
        inst.post = AsyncMock(
            side_effect=[
                _make_response({}, status_code=502),
                _make_response({}, status_code=502),
                _make_response(_success_body("ok")),
            ]
        )
        return inst

    with patch.object(a2a_client.httpx, "AsyncClient", new=factory_with_responses):
        result = await a2a_client.invoke_agent(prompt="hi", task_id="t1", max_retries=3)

    assert result == "ok"
    # Exactly one client across the 3 attempts.
    assert len(ctor_kwargs) == 1, f"expected 1 AsyncClient construction, got {len(ctor_kwargs)}"
    # And that one client absorbed all three .post() calls.
    assert instances[0].post.await_count == 3


# ---------------------------------------------------------------------------
# invoke_agent — A2A error envelope handling is preserved
# ---------------------------------------------------------------------------

async def test_a2a_error_envelope_raises_runtime_error():
    """A 200 response with ``{"error": ...}`` body is a logical failure,
    not a transport one — surface it as RuntimeError without retrying.
    """
    body = {"error": {"code": -32000, "message": "agent unavailable"}}
    mock_post = AsyncMock(return_value=_make_response(body))
    with patch.object(a2a_client, "_post_once", new=mock_post):
        with pytest.raises(RuntimeError, match="A2A error from supervisor"):
            await a2a_client.invoke_agent(prompt="hi", task_id="t1")

    assert mock_post.await_count == 1
