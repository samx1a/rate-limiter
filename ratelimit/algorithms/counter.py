"""Step 1: the dumbest limiter that works.

Each key gets a counter. Once it reaches ``limit``, every later request is blocked,
forever. There is no notion of time, so this is only useful as a baseline (or for
one-off quotas such as "3 free trials"). Every other limiter adds time to this idea.
"""

from __future__ import annotations

from ratelimit.algorithms.base import InMemoryLimiter
from ratelimit.decision import INF, Decision


class NaiveCounter(InMemoryLimiter[int]):
    """key -> requests admitted so far."""

    def __init__(self, limit: int) -> None:
        super().__init__(limit)

    def _hit(self, key: str, cost: int) -> Decision:
        used = self._state.get(key, 0)
        if used + cost > self._limit:
            # Blocked requests are not counted.
            return Decision(False, self._limit, self._limit - used, INF, retry_after=INF)
        used += cost
        self._state[key] = used
        return Decision(True, self._limit, self._limit - used, INF)

    def reset(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)
