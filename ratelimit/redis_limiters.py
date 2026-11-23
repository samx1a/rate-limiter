"""Rate limiters whose state lives in Redis, shared by every app server.

Each limiter is one Lua script that reads, decides and writes in a single atomic
step. Python never does "GET, then decide, then SET": two servers doing that at the
same moment would both see the old value and both admit a request (a race).

Time comes from the Redis server clock by default, so app servers with slightly
different clocks still agree. Tests can pass ``now_ms`` to control time.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from importlib import resources
from typing import Any

import redis

from ratelimit.decision import Decision, validate_cost

NowMs = Callable[[], float]


def load_script(client: redis.Redis, name: str) -> Any:
    source = resources.files("ratelimit.lua").joinpath(f"{name}.lua").read_text()
    # register_script runs via EVALSHA and reloads the script if Redis lost it (NOSCRIPT).
    return client.register_script(source)


def _ms_to_s(value: Any) -> float:
    ms = float(value)
    return math.inf if ms < 0 else ms / 1000


class RedisTokenBucket:
    def __init__(
        self,
        client: redis.Redis,
        capacity: int,
        refill_rate: float,
        *,
        prefix: str = "rl",
        now_ms: NowMs | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be > 0 tokens per second")
        self._capacity = capacity
        self._rate = refill_rate
        self._prefix = prefix
        self._now_ms = now_ms
        self._script = load_script(client, "token_bucket")

    @property
    def limit(self) -> int:
        return self._capacity

    def redis_key(self, key: str) -> str:
        return f"{self._prefix}:tb:{key}"

    def hit(self, key: str, cost: int = 1) -> Decision:
        validate_cost(cost)
        now = "" if self._now_ms is None else repr(float(self._now_ms()))
        allowed, remaining, retry_ms, reset_ms = self._script(
            keys=[self.redis_key(key)], args=[self._capacity, repr(self._rate), cost, now]
        )
        if int(allowed):
            return Decision(True, self._capacity, int(remaining), _ms_to_s(reset_ms))
        return Decision(
            False,
            self._capacity,
            int(remaining),
            _ms_to_s(reset_ms),
            retry_after=_ms_to_s(retry_ms),
        )
