"""A circuit breaker for the limiter's store.

When Redis is down, every limit check waits for the socket timeout (250 ms by
default) before failing open. That's 250 ms added to *every* request for as long as
the outage lasts, plus a pile of threads stuck waiting. A breaker stops calling Redis
after a few failures in a row and fails instantly instead, then lets one trial call
through every ``cooldown`` seconds to see if Redis is back.

States:
    closed     normal. Count consecutive failures; ``threshold`` of them opens it.
    open       don't call Redis. After ``cooldown`` seconds, move to half-open.
    half-open  let exactly one trial call through. Success closes the breaker;
               failure opens it for another ``cooldown``.
"""

from __future__ import annotations

import threading
from enum import StrEnum

from ratelimit.clock import Clock, MonotonicClock


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self, threshold: int = 3, cooldown: float = 5.0, clock: Clock | None = None
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        if cooldown <= 0:
            raise ValueError("cooldown must be > 0 seconds")
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock or MonotonicClock()
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_running = False

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state()

    def _state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock.now() - self._opened_at < self._cooldown:
            return BreakerState.OPEN
        return BreakerState.HALF_OPEN

    def allow(self) -> bool:
        """May the caller try the store now? A True in half-open state is the one
        trial call, and the caller must report how it went."""
        with self._lock:
            state = self._state()
            if state is BreakerState.CLOSED:
                return True
            if state is BreakerState.HALF_OPEN and not self._trial_running:
                self._trial_running = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._trial_running = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._trial_running or self._failures >= self._threshold:
                self._opened_at = self._clock.now()
            self._trial_running = False
