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

from ratelimit.algorithms.base import WindowedLimiter
from ratelimit.decision import Decision


class FixedWindow(WindowedLimiter[tuple[int, int]]):
    """key -> (index of the window the count belongs to, count)."""

    def _hit(self, key: str, cost: int) -> Decision:
        now = self._clock.now()
        index = math.floor(now / self._window)
        window_ends_in = (index + 1) * self._window - now

        stored_index, used = self._state.get(key, (index, 0))
        if stored_index != index:
            used = 0  # a new window has started

        if used + cost > self._limit:
            retry = math.inf if cost > self._limit else window_ends_in  # inf: can never fit
            return Decision(
                False, self._limit, self._limit - used, window_ends_in, retry_after=retry
            )
        used += cost
        self._state[key] = (index, used)
        return Decision(True, self._limit, self._limit - used, window_ends_in)

    def _is_idle(self, state: tuple[int, int], now: float) -> bool:
        return state[0] < math.floor(now / self._window)  # its window is over
