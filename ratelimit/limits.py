"""Turn config strings like ``"10/10s"`` into limiters.

Limits are written as ``<requests>/<period>``: ``10/s``, ``100/1m``, ``5/30s``,
``1000/1h``, ``20000/1d``. Keeping limits in config (not code) lets operators tune them
per environment without a deploy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ratelimit.algorithms.counter import NaiveCounter
from ratelimit.algorithms.fixed_window import FixedWindow
from ratelimit.algorithms.sliding_window import SlidingWindowLog
from ratelimit.algorithms.token_bucket import TokenBucket
from ratelimit.clock import Clock
from ratelimit.decision import RateLimiter

_UNITS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400, "day": 86400}
_PATTERN = re.compile(r"^\s*(\d+)\s*/\s*(\d+(?:\.\d+)?)?\s*([a-z]+)\s*$")


class Algorithm(StrEnum):
    COUNTER = "counter"
    FIXED_WINDOW = "fixed_window"
    SLIDING_WINDOW = "sliding_window"
    TOKEN_BUCKET = "token_bucket"


@dataclass(frozen=True, slots=True)
class LimitSpec:
    """``requests`` per ``period`` seconds."""

    requests: int
    period: float

    def __post_init__(self) -> None:
        if self.requests < 1:
            raise ValueError("a limit must allow at least 1 request")
        if self.period <= 0:
            raise ValueError("a limit period must be > 0 seconds")

    @property
    def rate(self) -> float:
        """Long-run requests per second."""
        return self.requests / self.period

    def __str__(self) -> str:
        period = f"{self.period:g}s"
        return f"{self.requests}/{period}"


def parse_limit(text: str) -> LimitSpec:
    match = _PATTERN.match(text.lower())
    if not match:
        raise ValueError(f"bad limit {text!r}; expected e.g. '10/s', '100/1m', '5/30s'")
    requests, amount, unit = match.groups()
    if unit not in _UNITS:
        raise ValueError(f"bad unit {unit!r} in {text!r}; use one of {sorted(_UNITS)}")
    return LimitSpec(int(requests), float(amount or 1) * _UNITS[unit])


def build_limiter(
    algorithm: Algorithm | str, spec: LimitSpec, clock: Clock | None = None
) -> RateLimiter:
    """Build an in-memory limiter. For token bucket, ``requests`` is the burst size and
    the bucket refills at ``requests / period`` per second."""
    match Algorithm(algorithm):
        case Algorithm.COUNTER:
            return NaiveCounter(spec.requests)
        case Algorithm.FIXED_WINDOW:
            return FixedWindow(spec.requests, spec.period, clock)
        case Algorithm.SLIDING_WINDOW:
            return SlidingWindowLog(spec.requests, spec.period, clock)
        case Algorithm.TOKEN_BUCKET:
            return TokenBucket(spec.requests, spec.rate, clock)
