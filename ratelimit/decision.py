"""The contract every limiter follows."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of one ``hit``.

    Attributes:
        allowed: whether the request may proceed.
        limit: the most requests this key can make in one burst.
        remaining: requests still allowed right now (never negative).
        reset_after: seconds until the full ``limit`` is available again.
            ``math.inf`` if the limiter never resets.
        retry_after: seconds until a blocked request could succeed. 0 when allowed.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_after: float
    retry_after: float = 0.0

    def __post_init__(self) -> None:
        if self.remaining < 0:
            raise ValueError("remaining must be >= 0")
        if self.allowed and self.retry_after != 0:
            raise ValueError("an allowed decision has no retry_after")


class RateLimiter(Protocol):
    """Anything that can decide whether one more request for ``key`` is allowed.

    ``hit`` checks and records in one step: if the request is allowed, its cost is
    counted; if it is blocked, nothing is recorded.
    """

    @property
    def limit(self) -> int: ...

    def hit(self, key: str, cost: int = 1) -> Decision: ...


def validate_cost(cost: int) -> None:
    if not isinstance(cost, int) or isinstance(cost, bool) or cost < 1:
        raise ValueError(f"cost must be a positive integer, got {cost!r}")


INF = math.inf
