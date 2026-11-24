"""What every in-memory limiter shares: one lock, one dict of per-key state.

Subclasses only write ``_hit``, the read-decide-write for one key. The base class
holds the lock around it, so no subclass can forget to (or take it twice).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from ratelimit.clock import Clock, MonotonicClock
from ratelimit.decision import Decision, validate_cost

S = TypeVar("S")

# Don't bother sweeping tiny maps.
MIN_SWEEP_SIZE = 1024


class InMemoryLimiter(ABC, Generic[S]):
    def __init__(self, limit: int, clock: Clock | None = None) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._clock = clock or MonotonicClock()
        self._state: dict[str, S] = {}
        # One lock guards the whole read-decide-write in hit(). The clock is read
        # inside the lock, so state is always updated in time order.
        self._lock = threading.Lock()
        self._sweep_at = MIN_SWEEP_SIZE

    @property
    def limit(self) -> int:
        return self._limit

    def hit(self, key: str, cost: int = 1) -> Decision:
        validate_cost(cost)
        with self._lock:
            decision = self._hit(key, cost)
            if len(self._state) >= self._sweep_at:
                self._auto_sweep()
            return decision

    def reset(self, key: str) -> None:
        """Forget ``key``: its next request sees the full limit."""
        with self._lock:
            self._state.pop(key, None)

    def sweep(self) -> int:
        """Drop every key whose state is the same as a missing key (bucket full again,
        window over, log empty) and return how many were dropped.

        Without this, an in-memory limiter remembers every key it has ever seen: one
        request from each of a million IPs is a million entries that never go away.
        """
        with self._lock:
            return self._sweep()

    def _sweep(self) -> int:
        now = self._clock.now()
        idle = [key for key, state in self._state.items() if self._is_idle(state, now)]
        for key in idle:
            del self._state[key]
        return len(idle)

    def _auto_sweep(self) -> None:
        """Sweep when the map has doubled since the last sweep.

        Same trick as a growable array: a sweep costs O(keys), but it only runs after
        the map has grown by as many keys as it holds, so the cost per hit averages
        out to O(1). No background thread needed. If most keys are active the sweep
        frees little and the next one waits for the map to double again, so a busy
        limiter is never swept on every hit.
        """
        self._sweep()
        self._sweep_at = max(MIN_SWEEP_SIZE, 2 * len(self._state))

    def __len__(self) -> int:
        """Keys currently held in memory."""
        return len(self._state)

    def _is_idle(self, state: S, now: float) -> bool:
        """True if forgetting this state would not change any future decision."""
        return False

    @abstractmethod
    def _hit(self, key: str, cost: int) -> Decision:
        """Decide and record one request. Called with the lock held."""


class WindowedLimiter(InMemoryLimiter[S]):
    """A limiter defined by ``limit`` per ``window`` seconds."""

    def __init__(self, limit: int, window: float, clock: Clock | None = None) -> None:
        super().__init__(limit, clock)
        if window <= 0:
            raise ValueError("window must be > 0 seconds")
        self._window = window
