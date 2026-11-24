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
from dataclasses import dataclass, field

from ratelimit.algorithms.base import WindowedLimiter
from ratelimit.decision import Decision


@dataclass(slots=True)
class _Log:
    entries: deque[tuple[float, int]] = field(default_factory=deque)  # (time, cost)
    total: int = 0  # sum of the costs in ``entries``


class SlidingWindowLog(WindowedLimiter[_Log]):
    """key -> log of admitted requests in the last window, oldest first."""

    def _hit(self, key: str, cost: int) -> Decision:
        now = self._clock.now()
        state = self._state.get(key)
        if state is None:
            state = self._state[key] = _Log()
        log = state.entries

        # Drop entries that have slid out of the window: [now - window, now).
        while log and log[0][0] + self._window <= now:
            state.total -= log.popleft()[1]

        if state.total + cost > self._limit:
            return Decision(
                False,
                self._limit,
                self._limit - state.total,
                self._reset_after(log, now),
                retry_after=self._retry_after(log, now, state.total + cost - self._limit, cost),
            )

        log.append((now, cost))
        state.total += cost
        return Decision(True, self._limit, self._limit - state.total, self._reset_after(log, now))

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
