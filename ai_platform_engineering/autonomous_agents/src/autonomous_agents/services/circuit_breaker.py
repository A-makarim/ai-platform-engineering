"""Per-supervisor-URL circuit breaker for the A2A client.

Why
---
``invoke_agent`` already retries transient supervisor failures (5xx +
transport errors) via tenacity. That's the right behaviour for a single
flaky request, but it hurts when the supervisor is *broken*: every
scheduled task spends its full retry budget hammering a downstream that
can't recover, multiplying load and turning a localised outage into a
self-DoS.

A circuit breaker fixes this by tracking *consecutive* failures across
calls and short-circuiting once a threshold is reached:

    CLOSED ──N consecutive failures──► OPEN
       ▲                                 │
       │                                 │ cooldown elapses
       │                                 ▼
       └────success on trial──────── HALF_OPEN ──failure──► OPEN

In the OPEN state, callers get an immediate ``CircuitBreakerOpenError``
without touching the network. After ``cooldown_seconds`` we move to
HALF_OPEN and let *one* trial request through; success closes the
circuit, failure re-opens it for another cooldown.

Design notes
------------
* **Per-URL keying.** A single autonomous-agents process can talk to
  multiple supervisor URLs (rare today but supported by config + tests),
  and one bad URL must not poison the others.
* **Counts only after retries are exhausted.** ``invoke_agent`` is the
  one that calls ``record_failure`` -- *not* tenacity's per-attempt
  hook. A request that succeeds on its second attempt should leave the
  breaker untouched, because the supervisor is plainly working.
* **Thread-safe via ``asyncio.Lock``.** All mutating operations take
  the lock so concurrent tasks can't race the state machine.
* **Disabled-by-default safety.** When ``enabled=False`` the breaker
  is a no-op pass-through; it never raises and never records state.
  Operators can flip the kill-switch via ``CIRCUIT_BREAKER_ENABLED=0``
  if the feature ever misbehaves in prod.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("autonomous_agents")


class CircuitState(str, Enum):
    """Externalised state name -- kept stable so logs/metrics can pivot on it."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(RuntimeError):
    """Raised when a call is short-circuited because the breaker is OPEN.

    Carries the URL and how many seconds remain in the cooldown so
    callers (and run-history rows) can show a useful message instead
    of a generic ``RuntimeError``.
    """

    def __init__(self, url: str, retry_after_seconds: float) -> None:
        self.url = url
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Supervisor circuit breaker is OPEN for {url}; "
            f"retry after ~{retry_after_seconds:.1f}s"
        )


@dataclass
class _BreakerStats:
    """Per-URL state. Kept private -- callers go through the manager."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CircuitBreaker:
    """Thread-safe (asyncio) circuit breaker keyed by supervisor URL.

    Parameters
    ----------
    failure_threshold:
        How many *consecutive* failed ``invoke_agent`` calls trip the
        breaker. A single failure followed by a success resets the
        counter to zero.
    cooldown_seconds:
        How long the breaker stays OPEN before allowing a HALF_OPEN
        trial. Should be long enough that the downstream has a real
        chance to recover (default 30s) but short enough that we don't
        wedge a healthy supervisor for an unnecessary outage window.
    enabled:
        Master kill-switch. ``False`` disables every method on this
        instance; ``before_call`` becomes a no-op and
        ``record_success`` / ``record_failure`` are ignored.
    clock:
        Indirection point for tests so they don't have to ``sleep``
        through the cooldown. Production code never overrides this.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        enabled: bool = True,
        clock=time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._enabled = enabled
        self._clock = clock
        self._stats: dict[str, _BreakerStats] = {}
        # Guards creation of per-URL stats only -- per-URL operations
        # use the per-URL lock for finer-grained concurrency.
        self._stats_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def _get_stats(self, url: str) -> _BreakerStats:
        # Fast path: already created. Avoids paying the global lock on
        # every call once the URL is in the dict.
        existing = self._stats.get(url)
        if existing is not None:
            return existing
        async with self._stats_lock:
            # Re-check inside the lock in case a concurrent caller beat us.
            existing = self._stats.get(url)
            if existing is None:
                existing = _BreakerStats()
                self._stats[url] = existing
        return existing

    async def before_call(self, url: str) -> None:
        """Gate a call. Raises ``CircuitBreakerOpenError`` if blocked.

        Side effects:
        * If we're OPEN and the cooldown has elapsed, transitions to
          HALF_OPEN so the very next call is the trial request.
        * No state change on the CLOSED happy path -- this is the hot
          path and must stay cheap.
        """
        if not self._enabled:
            return

        stats = await self._get_stats(url)
        async with stats.lock:
            if stats.state is CircuitState.CLOSED:
                return
            if stats.state is CircuitState.OPEN:
                assert stats.opened_at is not None  # noqa: S101 -- invariant
                elapsed = self._clock() - stats.opened_at
                remaining = self._cooldown - elapsed
                if remaining > 0:
                    raise CircuitBreakerOpenError(url, retry_after_seconds=remaining)
                # Cooldown elapsed -- move to HALF_OPEN and let this caller
                # be the trial request. Do not raise.
                logger.info(
                    "Circuit breaker for %s: OPEN -> HALF_OPEN after %.1fs cooldown",
                    url,
                    elapsed,
                )
                stats.state = CircuitState.HALF_OPEN
                stats.opened_at = None
            # HALF_OPEN: let this single caller through; no concurrent
            # trial protection because invoke_agent calls are independent
            # and a second concurrent caller will simply also count as
            # part of the trial window. The first to record_failure
            # re-opens the breaker.

    async def record_success(self, url: str) -> None:
        """Reset the failure counter and close the breaker if it was tripped."""
        if not self._enabled:
            return
        stats = await self._get_stats(url)
        async with stats.lock:
            if stats.consecutive_failures > 0 or stats.state is not CircuitState.CLOSED:
                logger.info(
                    "Circuit breaker for %s: closing (was %s, %d consecutive failures)",
                    url,
                    stats.state.value,
                    stats.consecutive_failures,
                )
            stats.consecutive_failures = 0
            stats.state = CircuitState.CLOSED
            stats.opened_at = None

    async def record_failure(self, url: str) -> CircuitState:
        """Note one *post-retry* failure. May trip the breaker.

        Returns the new state so callers can log it. We always return
        the state even when the breaker is disabled (CLOSED) so
        downstream code doesn't have to special-case the kill-switch.
        """
        if not self._enabled:
            return CircuitState.CLOSED

        stats = await self._get_stats(url)
        async with stats.lock:
            stats.consecutive_failures += 1
            # HALF_OPEN trial failed -> straight back to OPEN with a fresh
            # cooldown. We don't wait for the threshold here because the
            # whole point of HALF_OPEN is "one shot to prove recovery".
            if stats.state is CircuitState.HALF_OPEN:
                stats.state = CircuitState.OPEN
                stats.opened_at = self._clock()
                logger.warning(
                    "Circuit breaker for %s: HALF_OPEN trial failed -> OPEN "
                    "(cooldown %.1fs)",
                    url,
                    self._cooldown,
                )
                return stats.state

            # CLOSED: trip only when we hit the consecutive-failure threshold.
            if (
                stats.state is CircuitState.CLOSED
                and stats.consecutive_failures >= self._threshold
            ):
                stats.state = CircuitState.OPEN
                stats.opened_at = self._clock()
                logger.warning(
                    "Circuit breaker for %s: tripped after %d consecutive "
                    "failures -> OPEN (cooldown %.1fs)",
                    url,
                    stats.consecutive_failures,
                    self._cooldown,
                )
            return stats.state

    async def state_for(self, url: str) -> CircuitState:
        """Read-only snapshot of the current state. Useful for tests / metrics."""
        if not self._enabled:
            return CircuitState.CLOSED
        stats = await self._get_stats(url)
        async with stats.lock:
            # Auto-transition to HALF_OPEN on read so dashboards reflect
            # reality without waiting for the next call.
            if stats.state is CircuitState.OPEN and stats.opened_at is not None:
                if self._clock() - stats.opened_at >= self._cooldown:
                    stats.state = CircuitState.HALF_OPEN
                    stats.opened_at = None
            return stats.state

    async def reset(self, url: str | None = None) -> None:
        """Test helper -- wipe state for one URL or all of them."""
        if url is None:
            async with self._stats_lock:
                self._stats.clear()
            return
        stats = await self._get_stats(url)
        async with stats.lock:
            stats.consecutive_failures = 0
            stats.state = CircuitState.CLOSED
            stats.opened_at = None


_breaker_singleton: CircuitBreaker | None = None
_singleton_lock = asyncio.Lock()


async def get_circuit_breaker() -> CircuitBreaker:
    """Lazy module-level singleton built from current ``Settings``.

    Built lazily (rather than at import time) so that ``Settings``
    overrides applied in tests via ``monkeypatch.setattr`` are picked
    up. Once built it is reused for the lifetime of the process; tests
    can call :func:`reset_circuit_breaker` to force a rebuild.
    """
    global _breaker_singleton
    if _breaker_singleton is not None:
        return _breaker_singleton
    async with _singleton_lock:
        if _breaker_singleton is None:
            # Imported here to avoid a circular import at module load.
            from autonomous_agents.config import get_settings

            settings = get_settings()
            _breaker_singleton = CircuitBreaker(
                failure_threshold=settings.circuit_breaker_failure_threshold,
                cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
                enabled=settings.circuit_breaker_enabled,
            )
    return _breaker_singleton


def reset_circuit_breaker() -> None:
    """Drop the singleton so the next ``get_circuit_breaker`` rebuilds it.

    Synchronous on purpose so it can be used from pytest fixtures
    without ``asyncio.run``.
    """
    global _breaker_singleton
    _breaker_singleton = None
