"""Step 2: fixed window.

Time is cut into back-to-back windows of ``window`` seconds (0-10s, 10-20s, ...).
Each key may make ``limit`` requests per window; the count starts over at each edge.

Why it lets bursts through at window edges
------------------------------------------
The limit is per *calendar* window, not per *any* ``window`` seconds. A client can
spend its whole quota at the very end of one window and its whole new quota at the
very start of the next. With ``limit=100, window=60s``:

    t=59.9s  100 requests  -> allowed (window 0-60 is not full yet)
    t=60.0s  100 requests  -> allowed (new window 60-120, count is back to 0)

That is 200 requests in 0.1 seconds, **2x the limit**, and it can repeat every
window. ``tests/test_fixed_window.py::test_edge_burst_lets_through_twice_the_limit``
shows it happening. The sliding window and token bucket limiters fix this.

Fixed window is still popular because it is cheap: one integer per key, and in Redis
one ``INCR`` with an expiry.
"""

from __future__ import annotations

import math

from ratelimit.clock import Clock, MonotonicClock
from ratelimit.decision import Decision, validate_cost


class FixedWindow:
    def __init__(self, limit: int, window: float, clock: Clock | None = None) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window <= 0:
            raise ValueError("window must be > 0 seconds")
        self._limit = limit
        self._window = window
        self._clock = clock or MonotonicClock()
        # key -> (index of the window the count belongs to, count)
        self._state: dict[str, tuple[int, int]] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def hit(self, key: str, cost: int = 1) -> Decision:
        validate_cost(cost)
        now = self._clock.now()
        index = math.floor(now / self._window)
        window_ends_in = (index + 1) * self._window - now

        stored_index, used = self._state.get(key, (index, 0))
        if stored_index != index:
            used = 0  # a new window has started

        if used + cost > self._limit:
            return Decision(
                False, self._limit, self._limit - used, window_ends_in, retry_after=window_ends_in
            )
        used += cost
        self._state[key] = (index, used)
        return Decision(True, self._limit, self._limit - used, window_ends_in)
