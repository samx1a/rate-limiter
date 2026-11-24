"""Step 4: token bucket with lazy refill.

Each key has a bucket holding up to ``capacity`` tokens. A request spends ``cost``
tokens; if there are not enough, it is blocked. Tokens flow back in at
``refill_rate`` per second, up to ``capacity``.

This allows short bursts (up to ``capacity``) while capping the long-run rate at
``refill_rate``, and it has no window edges, so there is no edge burst.

Lazy refill: nothing runs in the background. Each bucket stores
``(tokens, last_refill_time)``. On every hit we add ``elapsed * refill_rate`` tokens
(capped at capacity) and then decide. An idle bucket costs nothing, and the result is
identical to a timer that refills continuously.

Floating point: refill math such as ``0.1 tokens/s * 10s`` can land on
``0.9999999999999999`` instead of 1. Comparisons use a tiny tolerance (``EPSILON``) so
a request is not blocked by rounding error.
"""

from __future__ import annotations

import math

from ratelimit.algorithms.base import InMemoryLimiter
from ratelimit.clock import Clock
from ratelimit.decision import Decision

EPSILON = 1e-9


class TokenBucket(InMemoryLimiter[tuple[float, float]]):
    """key -> (tokens, time of the last refill)."""

    def __init__(self, capacity: int, refill_rate: float, clock: Clock | None = None) -> None:
        super().__init__(capacity, clock)
        if refill_rate <= 0:
            raise ValueError("refill_rate must be > 0 tokens per second")
        self._rate = refill_rate

    @property
    def refill_rate(self) -> float:
        return self._rate

    def _hit(self, key: str, cost: int) -> Decision:
        now = self._clock.now()
        tokens, last = self._state.get(key, (float(self._limit), now))

        # Lazy refill: credit the time since the last hit, capped at capacity.
        elapsed = max(0.0, now - last)
        tokens = min(float(self._limit), tokens + elapsed * self._rate)

        allowed = tokens + EPSILON >= cost
        if allowed:
            tokens = max(0.0, tokens - cost)
        self._state[key] = (tokens, now)

        remaining = math.floor(tokens + EPSILON)
        reset_after = (self._limit - tokens) / self._rate
        if allowed:
            return Decision(True, self._limit, remaining, reset_after)
        retry_after = math.inf if cost > self._limit else (cost - tokens) / self._rate
        return Decision(False, self._limit, remaining, reset_after, retry_after=retry_after)

    def _is_idle(self, state: tuple[float, float], now: float) -> bool:
        tokens, last = state
        return tokens + max(0.0, now - last) * self._rate + EPSILON >= self._limit  # full
