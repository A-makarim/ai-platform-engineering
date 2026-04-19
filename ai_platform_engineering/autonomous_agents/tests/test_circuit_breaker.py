# Copyright CNOE Contributors (https://cnoe.io)
# SPDX-License-Identifier: Apache-2.0

"""Unit + integration tests for the supervisor circuit breaker (IMP-16).

The unit tests below drive the breaker state machine directly with a
fake ``clock`` so they don't sleep. The integration tests at the bottom
wire the real breaker into ``invoke_agent`` (with a mocked supervisor)
and verify that:

* a request that succeeds on retry leaves the breaker untouched
  (we count *post-retry* failures only),
* once the threshold is reached the breaker short-circuits subsequent
  calls without touching the network,
* after the cooldown elapses one trial request is allowed and a
  successful trial closes the breaker.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from autonomous_agents.config import Settings, get_settings
from autonomous_agents.services import a2a_client
from autonomous_agents.services import circuit_breaker as cb_mod
from autonomous_agents.services.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
)


class _FakeClock:
    """Monotonic clock substitute. Tests advance time explicitly."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Pure state-machine tests
# ---------------------------------------------------------------------------


async def test_starts_closed():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10)
    assert await breaker.state_for("u") is CircuitState.CLOSED
    # No-op when CLOSED -- must not raise.
    await breaker.before_call("u")


async def test_records_failures_below_threshold_stays_closed():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
    await breaker.record_failure("u")
    await breaker.record_failure("u")
    assert await breaker.state_for("u") is CircuitState.CLOSED
    await breaker.before_call("u")  # still allowed through


async def test_trips_open_at_threshold():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10)
    await breaker.record_failure("u")
    state = await breaker.record_failure("u")
    assert state is CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        await breaker.before_call("u")
    # Error carries the URL and remaining cooldown so callers can show
    # something useful instead of a bare RuntimeError.
    assert exc_info.value.url == "u"
    assert 0 < exc_info.value.retry_after_seconds <= 10


async def test_success_resets_failure_counter():
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10)
    await breaker.record_failure("u")
    await breaker.record_failure("u")
    await breaker.record_success("u")
    # Need 3 *consecutive* failures again -- counter was zeroed.
    await breaker.record_failure("u")
    await breaker.record_failure("u")
    assert await breaker.state_for("u") is CircuitState.CLOSED


async def test_open_blocks_until_cooldown_then_half_opens():
    clock = _FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
    await breaker.record_failure("u")
    assert await breaker.state_for("u") is CircuitState.OPEN

    # Mid-cooldown: still blocked.
    clock.advance(15)
    with pytest.raises(CircuitBreakerOpenError):
        await breaker.before_call("u")

    # Cooldown elapsed: next before_call transitions OPEN -> HALF_OPEN
    # and DOES NOT raise (the caller is the trial request).
    clock.advance(20)  # now 35s past trip
    await breaker.before_call("u")
    assert await breaker.state_for("u") is CircuitState.HALF_OPEN


async def test_half_open_failure_reopens_with_fresh_cooldown():
    clock = _FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=clock)
    await breaker.record_failure("u")
    clock.advance(15)
    await breaker.before_call("u")  # -> HALF_OPEN

    # Trial fails -> straight back to OPEN, ignoring the threshold (the
    # whole point of HALF_OPEN is "one shot to prove recovery").
    state = await breaker.record_failure("u")
    assert state is CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpenError):
        await breaker.before_call("u")


async def test_half_open_success_closes_breaker():
    clock = _FakeClock()
    # threshold=2 so we can demonstrate that a single new failure after
    # recovery does NOT re-trip (counter was reset to zero by the success).
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10, clock=clock)
    await breaker.record_failure("u")
    await breaker.record_failure("u")  # threshold met -> OPEN
    assert await breaker.state_for("u") is CircuitState.OPEN
    clock.advance(15)
    await breaker.before_call("u")  # -> HALF_OPEN
    await breaker.record_success("u")
    assert await breaker.state_for("u") is CircuitState.CLOSED
    # A single new failure must not re-open the freshly-closed breaker --
    # the counter was reset by record_success.
    await breaker.record_failure("u")
    assert await breaker.state_for("u") is CircuitState.CLOSED


async def test_per_url_isolation():
    """A bad URL must not poison the breaker for a healthy one."""
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10)
    await breaker.record_failure("bad")
    assert await breaker.state_for("bad") is CircuitState.OPEN
    assert await breaker.state_for("good") is CircuitState.CLOSED
    # Healthy URL is unaffected.
    await breaker.before_call("good")


async def test_disabled_breaker_is_passthrough():
    """``enabled=False`` makes every method a no-op. before_call never raises,
    record_failure never trips, state_for always returns CLOSED.
    """
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, enabled=False)
    for _ in range(20):
        await breaker.record_failure("u")
    assert await breaker.state_for("u") is CircuitState.CLOSED
    await breaker.before_call("u")  # must not raise


async def test_state_for_auto_transitions_open_to_half_open():
    """Reading state after cooldown reflects HALF_OPEN without a call.

    Useful so dashboards / metrics don't lie about the breaker state
    just because nobody has called in to trigger the transition.
    """
    clock = _FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=clock)
    await breaker.record_failure("u")
    assert await breaker.state_for("u") is CircuitState.OPEN
    clock.advance(6)
    assert await breaker.state_for("u") is CircuitState.HALF_OPEN


def test_invalid_construction_rejected():
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0, cooldown_seconds=10)
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=1, cooldown_seconds=0)


# ---------------------------------------------------------------------------
# Settings integration -- finite-number guard mirrors a2a_* validators
# ---------------------------------------------------------------------------


def test_settings_rejects_non_finite_cooldown():
    # ``inf > 0`` is True so pydantic's ``gt=0`` constraint accepts it;
    # our validator catches it with a "finite" message.
    with pytest.raises(ValueError, match="finite"):
        Settings(circuit_breaker_cooldown_seconds=float("inf"))
    # ``nan > 0`` is False, so pydantic's ``gt=0`` rejects nan first.
    # Either error is acceptable as long as the construction fails.
    with pytest.raises(ValueError):
        Settings(circuit_breaker_cooldown_seconds=float("nan"))


# ---------------------------------------------------------------------------
# Wiring: invoke_agent uses the breaker correctly
# ---------------------------------------------------------------------------


def _make_response(json_body: dict[str, Any], status_code: int = 200) -> httpx.Response:
    request = httpx.Request("POST", "http://supervisor.local")
    return httpx.Response(status_code, json=json_body, request=request)


def _success_body(text: str = "ok") -> dict[str, Any]:
    return {"result": {"artifacts": [{"parts": [{"kind": "text", "text": text}]}]}}


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://supervisor.local")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"{status_code}", request=request, response=response)


@pytest.fixture
def _strict_breaker_settings():
    """Tight-threshold settings so we can trip the breaker in 2 failures.

    Patches both ``a2a_client.get_settings`` (used to compute timeouts /
    retries / supervisor URL) AND ``cb_mod.get_circuit_breaker`` (used
    to gate the call) so the singleton honours the same tight thresholds
    instead of whatever defaults a previous test left on the cache.
    """
    get_settings.cache_clear()
    cb_mod.reset_circuit_breaker()
    fast = Settings(
        a2a_retry_backoff_initial_seconds=0.0,
        a2a_retry_backoff_max_seconds=0.001,
        a2a_max_retries=1,
        a2a_timeout_seconds=10.0,
        circuit_breaker_enabled=True,
        circuit_breaker_failure_threshold=2,
        circuit_breaker_cooldown_seconds=30.0,
    )
    breaker = CircuitBreaker(
        failure_threshold=fast.circuit_breaker_failure_threshold,
        cooldown_seconds=fast.circuit_breaker_cooldown_seconds,
        enabled=fast.circuit_breaker_enabled,
    )

    async def _get_breaker():
        return breaker

    with patch.object(a2a_client, "get_settings", return_value=fast), patch.object(
        a2a_client, "get_circuit_breaker", new=_get_breaker
    ):
        # Stash the breaker on the settings object so tests can inspect it
        # without re-calling get_circuit_breaker().
        fast._test_breaker = breaker  # type: ignore[attr-defined]
        yield fast
    get_settings.cache_clear()
    cb_mod.reset_circuit_breaker()


async def test_success_on_retry_does_not_count_as_breaker_failure(
    _strict_breaker_settings,
):
    """Per the design: only *post-retry* failures count.

    A request that 5xx's once and then succeeds is exactly the kind of
    flakiness retries exist for; it should not move us closer to OPEN.
    """
    breaker = _strict_breaker_settings._test_breaker
    mock_post = AsyncMock(
        side_effect=[_http_error(503), _make_response(_success_body("ok"))]
    )
    with patch.object(a2a_client, "_post_once", new=mock_post):
        result = await a2a_client.invoke_agent(prompt="hi", task_id="t1")

    assert result == "ok"
    assert (
        await breaker.state_for(_strict_breaker_settings.supervisor_url)
        is CircuitState.CLOSED
    )


async def test_breaker_trips_after_consecutive_fully_failed_invocations(
    _strict_breaker_settings,
):
    """Two complete invoke_agent calls that exhaust their retry budget
    should trip the breaker (threshold=2), and the third call should
    short-circuit *without* hitting the network.
    """
    breaker = _strict_breaker_settings._test_breaker
    mock_post = AsyncMock(side_effect=_http_error(503))
    with patch.object(a2a_client, "_post_once", new=mock_post):
        with pytest.raises(httpx.HTTPStatusError):
            await a2a_client.invoke_agent(prompt="hi", task_id="t1")
        with pytest.raises(httpx.HTTPStatusError):
            await a2a_client.invoke_agent(prompt="hi", task_id="t1")

    assert mock_post.await_count == 4
    assert (
        await breaker.state_for(_strict_breaker_settings.supervisor_url)
        is CircuitState.OPEN
    )

    with patch.object(a2a_client, "_post_once", new=mock_post):
        with pytest.raises(CircuitBreakerOpenError):
            await a2a_client.invoke_agent(prompt="hi", task_id="t1")
    assert mock_post.await_count == 4


async def test_breaker_recovers_after_cooldown_with_successful_trial(
    _strict_breaker_settings,
):
    breaker = _strict_breaker_settings._test_breaker

    # Trip the breaker.
    fail_post = AsyncMock(side_effect=_http_error(503))
    with patch.object(a2a_client, "_post_once", new=fail_post):
        with pytest.raises(httpx.HTTPStatusError):
            await a2a_client.invoke_agent(prompt="hi", task_id="t1")
        with pytest.raises(httpx.HTTPStatusError):
            await a2a_client.invoke_agent(prompt="hi", task_id="t1")
    assert (
        await breaker.state_for(_strict_breaker_settings.supervisor_url)
        is CircuitState.OPEN
    )

    # Fast-forward "time" by mutating the breaker's internal clock.
    # (The singleton uses time.monotonic; we cheat by manually walking
    # back the opened_at timestamp instead, which has the same effect.)
    stats = await breaker._get_stats(_strict_breaker_settings.supervisor_url)
    assert stats.opened_at is not None
    stats.opened_at -= _strict_breaker_settings.circuit_breaker_cooldown_seconds + 1

    # Trial call succeeds -> breaker should close.
    ok_post = AsyncMock(return_value=_make_response(_success_body("recovered")))
    with patch.object(a2a_client, "_post_once", new=ok_post):
        result = await a2a_client.invoke_agent(prompt="hi", task_id="t1")
    assert result == "recovered"
    assert (
        await breaker.state_for(_strict_breaker_settings.supervisor_url)
        is CircuitState.CLOSED
    )


async def test_disabled_breaker_does_not_short_circuit():
    """``CIRCUIT_BREAKER_ENABLED=0`` makes the feature inert.

    Tight failure_threshold=1 with disabled=True -- if the kill-switch
    is wired correctly, even sustained 5xx storms reach the network.
    """
    get_settings.cache_clear()
    cb_mod.reset_circuit_breaker()
    settings = Settings(
        a2a_retry_backoff_initial_seconds=0.0,
        a2a_retry_backoff_max_seconds=0.001,
        a2a_max_retries=0,
        circuit_breaker_enabled=False,
        circuit_breaker_failure_threshold=1,
    )
    breaker = CircuitBreaker(
        failure_threshold=settings.circuit_breaker_failure_threshold,
        cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        enabled=settings.circuit_breaker_enabled,
    )

    async def _get_breaker():
        return breaker

    with patch.object(a2a_client, "get_settings", return_value=settings), patch.object(
        a2a_client, "get_circuit_breaker", new=_get_breaker
    ):
        mock_post = AsyncMock(side_effect=_http_error(503))
        with patch.object(a2a_client, "_post_once", new=mock_post):
            for _ in range(5):
                with pytest.raises(httpx.HTTPStatusError):
                    await a2a_client.invoke_agent(prompt="hi", task_id="t1")
            assert mock_post.await_count == 5
    cb_mod.reset_circuit_breaker()
    get_settings.cache_clear()


async def test_4xx_does_not_trip_breaker(_strict_breaker_settings):
    """A 4xx is caller-fault and is *not* a sign the supervisor is unhealthy.

    Every well-formed-but-rejected request must not move us toward OPEN
    or a misconfigured task (bad auth header, wrong path) would
    self-DoS its own breaker entry. Only failures that ``_is_retryable_exception``
    treats as transient (5xx + transport errors) count toward the trip
    threshold; 4xx propagates but leaves the breaker at zero.
    """
    breaker = _strict_breaker_settings._test_breaker
    mock_post = AsyncMock(side_effect=_http_error(400))
    with patch.object(a2a_client, "_post_once", new=mock_post):
        for _ in range(5):
            with pytest.raises(httpx.HTTPStatusError):
                await a2a_client.invoke_agent(prompt="hi", task_id="t1")

    # All 5 calls reached the network (no retry on 4xx, no breaker block).
    assert mock_post.await_count == 5
    assert (
        await breaker.state_for(_strict_breaker_settings.supervisor_url)
        is CircuitState.CLOSED
    )
