"""Step 3: sliding window (sliding log).

Instead of calendar windows, look back exactly ``window`` seconds from *now* and count
what was admitted in that span. A request is allowed if that count plus its cost stays
within ``limit``. This removes the fixed window's edge burst: at any instant, the last
``window`` seconds never contain more than ``limit`` admitted requests.

How it is stored: per key, a queue of ``(time, cost)`` for admitted requests, oldest
first. Entries older than the window are dropped on each hit. Because admitted cost in
the queue never exceeds ``limit``, the queue holds at most ``limit`` entries, so memory
per key is bounded. Idle keys keep their entries until they are hit again; a long-lived
in-memory store would add a periodic sweep or an LRU cap. The Redis version uses TTLs.

Trade-off vs. the "sliding window counter": the counter keeps only two numbers per key
(this window's and last window's counts) and *estimates* the sliding count by
weighting the previous window. That uses less memory but can be off near edges. The
log is exact, which is what we want for a reference implementation; at very large
limits (say 100k/hour per key) the counter is the better choice.
"""

from __future__ import annotations

import math
from collections import deque

from ratelimit.clock import Clock, MonotonicClock
from ratelimit.decision import Decision, validate_cost


class SlidingWindowLog:
    def __init__(self, limit: int, window: float, clock: Clock | None = None) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window <= 0:
            raise ValueError("window must be > 0 seconds")
        self._limit = limit
        self._window = window
        self._clock = clock or MonotonicClock()
        self._logs: dict[str, deque[tuple[float, int]]] = {}
        self._totals: dict[str, int] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def hit(self, key: str, cost: int = 1) -> Decision:
        validate_cost(cost)
        now = self._clock.now()
        log = self._logs.setdefault(key, deque())
        total = self._totals.get(key, 0)

        # Drop entries that have slid out of the window: [now - window, now).
        while log and log[0][0] + self._window <= now:
            total -= log.popleft()[1]

        if total + cost > self._limit:
            self._totals[key] = total
            return Decision(
                False,
                self._limit,
                self._limit - total,
                self._reset_after(log, now),
                retry_after=self._retry_after(log, now, total + cost - self._limit, cost),
            )

        log.append((now, cost))
        total += cost
        self._totals[key] = total
        return Decision(True, self._limit, self._limit - total, self._reset_after(log, now))

    def _reset_after(self, log: deque[tuple[float, int]], now: float) -> float:
        """Time until every entry has expired, i.e. the full limit is back."""
        return log[-1][0] + self._window - now if log else 0.0

    def _retry_after(
        self, log: deque[tuple[float, int]], now: float, must_free: int, cost: int
    ) -> float:
        """Time until enough old entries expire to fit ``cost``."""
        if cost > self._limit:
            return math.inf  # can never fit
        freed = 0
        for stamp, entry_cost in log:
            freed += entry_cost
            if freed >= must_free:
                return stamp + self._window - now
        return 0.0  # unreachable when blocked, kept for safety
