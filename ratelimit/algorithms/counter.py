"""Step 1: the dumbest limiter that works.

Each key gets a counter. Once it reaches ``limit``, every later request is blocked,
forever. There is no notion of time, so this is only useful as a baseline (or for
one-off quotas such as "3 free trials"). Every other limiter adds time to this idea.
"""

from __future__ import annotations

import threading

from ratelimit.decision import INF, Decision, validate_cost


class NaiveCounter:
    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._counts: dict[str, int] = {}
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
        used = self._counts.get(key, 0)
        if used + cost > self._limit:
            # Blocked requests are not counted.
            return Decision(False, self._limit, self._limit - used, INF, retry_after=INF)
        used += cost
        self._counts[key] = used
        return Decision(True, self._limit, self._limit - used, INF)

    def reset(self, key: str) -> None:
        self._counts.pop(key, None)
