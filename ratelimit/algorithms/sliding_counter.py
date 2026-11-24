"""Step 3b: sliding window counter.

The sliding log (``sliding_window.py``) is exact but stores one entry per admitted
request. This keeps two integers per key instead: the count in the current fixed
window and the count in the previous one. It then *estimates* how many requests fall
in the last ``window`` seconds by assuming the previous window's requests were spread
evenly across it:

    estimate = previous * (fraction of the previous window still in view) + current

Example with a 60s window, 30s into the current window: half of the previous window
is still inside "the last 60 seconds", so 40 previous + 10 current is counted as
``40 * 0.5 + 10 = 30``.

Accuracy: the estimate is exact when traffic is even and off when it isn't. If the
previous window's requests all came at its very end, the estimate undercounts and a
client can get up to about 2x the limit into one sliding window (the same worst case
as fixed window, but it needs a much more contrived pattern). If they all came at its
start, the estimate overcounts and blocks a bit early. In practice errors are small;
Cloudflare measured about 0.003% of requests decided wrongly with this method.

What you get for that: O(1) memory per key no matter how big the limit is, which is
why this is the usual choice for limits like 100,000 per hour.
"""

from __future__ import annotations

import math
import threading

from ratelimit.clock import Clock, MonotonicClock
from ratelimit.decision import Decision, validate_cost

EPSILON = 1e-9


class SlidingWindowCounter:
    def __init__(self, limit: int, window: float, clock: Clock | None = None) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window <= 0:
            raise ValueError("window must be > 0 seconds")
        self._limit = limit
        self._window = window
        self._clock = clock or MonotonicClock()
        # key -> (index of the current window, its count, the previous window's count)
        self._state: dict[str, tuple[int, int, int]] = {}
        # One lock guards the whole read-decide-write in hit(). The clock is read
        # inside the lock, so state is always updated in time order.
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def hit(self, key: str, cost: int = 1) -> Decision:
        validate_cost(cost)
        with self._lock:
            return self._hit(key, cost)

    def _hit(self, key: str, cost: int) -> Decision:
        now = self._clock.now()
        index = math.floor(now / self._window)
        stored, current, previous = self._state.get(key, (index, 0, 0))
        if stored == index - 1:
            current, previous = 0, current  # one window later: current becomes previous
        elif stored < index - 1:
            current, previous = 0, 0  # idle for over a window: nothing is in view

        elapsed = now - index * self._window
        weight = 1 - elapsed / self._window  # share of the previous window still in view
        estimate = previous * weight + current

        if estimate + cost > self._limit + EPSILON:
            self._state[key] = (index, current, previous)
            return Decision(
                False,
                self._limit,
                self._remaining(estimate),
                self._reset_after(index, current, previous, now),
                retry_after=self._retry_after(index, current, previous, now, cost),
            )

        current += cost
        self._state[key] = (index, current, previous)
        return Decision(
            True,
            self._limit,
            self._remaining(estimate + cost),
            self._reset_after(index, current, previous, now),
        )

    def _remaining(self, estimate: float) -> int:
        return max(0, math.floor(self._limit - estimate + EPSILON))

    def _reset_after(self, index: int, current: int, previous: int, now: float) -> float:
        """Time until the estimate is back to 0, i.e. the full limit is available."""
        if current:
            # The current count only fully leaves view at the end of the next window.
            return (index + 2) * self._window - now
        if previous:
            return (index + 1) * self._window - now
        return 0.0

    def _retry_after(self, index: int, current: int, previous: int, now: float, cost: int) -> float:
        """Time until ``estimate + cost <= limit``.

        The estimate only falls as the previous window slides out of view, at
        ``previous / window`` per second, and drops once more when the window rolls
        over (current becomes previous).
        """
        if cost > self._limit:
            return math.inf
        start = index * self._window
        room = self._limit - current - cost  # what the previous window may still add
        if room >= 0:
            # Still this window: wait for previous * weight to fall to ``room``.
            fraction = 1 - room / previous
            return max(0.0, start + fraction * self._window - now)
        # Only after the rollover, once ``current`` has become the previous count.
        room = self._limit - cost
        fraction = max(0.0, 1 - room / current)
        return start + self._window + fraction * self._window - now
