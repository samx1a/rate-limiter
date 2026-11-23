"""Time sources.

Limiters never call ``time`` directly. They take a ``Clock`` so tests can move time
by hand instead of sleeping, which keeps tests fast and deterministic.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds as a float. Only differences matter, not the absolute value."""
        ...


class MonotonicClock:
    """Real time that never jumps backwards (unaffected by NTP or DST changes)."""

    def now(self) -> float:
        return time.monotonic()


class FakeClock:
    """A clock you move by hand. For tests and simulations."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time only moves forward")
        self._now += seconds

    def set(self, value: float) -> None:
        if value < self._now:
            raise ValueError("time only moves forward")
        self._now = value
