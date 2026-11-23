"""Rate limiters whose state lives in Redis, shared by every app server.

Each limiter is one Lua script that reads, decides and writes in a single atomic
step. Python never does "GET, then decide, then SET": two servers doing that at the
same moment would both see the old value and both admit a request (a race).

Time comes from the Redis server clock by default, so app servers with slightly
different clocks still agree. Tests can pass ``now_ms`` to control time.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Sequence
from importlib import resources
from typing import Any

import redis

from ratelimit.decision import Decision, RateLimiter, validate_cost
from ratelimit.limits import Algorithm, LimitSpec

NowMs = Callable[[], float]


def load_script(client: redis.Redis, name: str) -> Any:
    source = resources.files("ratelimit.lua").joinpath(f"{name}.lua").read_text()
    # register_script runs via EVALSHA and reloads the script if Redis lost it (NOSCRIPT).
    return client.register_script(source)


def _ms_to_s(value: Any) -> float:
    ms = float(value)
    return math.inf if ms < 0 else ms / 1000


class _RedisLimiter:
    """Shared plumbing: key naming, the clock argument, decoding the script reply."""

    script_name: str
    key_tag: str

    def __init__(
        self, client: redis.Redis, limit: int, *, prefix: str, now_ms: NowMs | None
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._prefix = prefix
        self._now_ms = now_ms
        self._script = load_script(client, self.script_name)

    @property
    def limit(self) -> int:
        return self._limit

    def redis_key(self, key: str) -> str:
        return f"{self._prefix}:{self.key_tag}:{key}"

    def _now_arg(self) -> str:
        return "" if self._now_ms is None else repr(float(self._now_ms()))

    def _run(self, key: str, args: Sequence[Any]) -> Decision:
        allowed, remaining, retry_ms, reset_ms = self._script(
            keys=[self.redis_key(key)], args=list(args)
        )
        reset = _ms_to_s(reset_ms)
        if int(allowed):
            return Decision(True, self._limit, int(remaining), reset)
        return Decision(False, self._limit, int(remaining), reset, retry_after=_ms_to_s(retry_ms))


class RedisTokenBucket(_RedisLimiter):
    script_name = "token_bucket"
    key_tag = "tb"

    def __init__(
        self,
        client: redis.Redis,
        capacity: int,
        refill_rate: float,
        *,
        prefix: str = "rl",
        now_ms: NowMs | None = None,
    ) -> None:
        if refill_rate <= 0:
            raise ValueError("refill_rate must be > 0 tokens per second")
        super().__init__(client, capacity, prefix=prefix, now_ms=now_ms)
        self._rate = refill_rate

    def hit(self, key: str, cost: int = 1) -> Decision:
        validate_cost(cost)
        return self._run(key, [self._limit, repr(self._rate), cost, self._now_arg()])


class RedisFixedWindow(_RedisLimiter):
    script_name = "fixed_window"
    key_tag = "fw"

    def __init__(
        self,
        client: redis.Redis,
        limit: int,
        window: float,
        *,
        prefix: str = "rl",
        now_ms: NowMs | None = None,
    ) -> None:
        if window <= 0:
            raise ValueError("window must be > 0 seconds")
        super().__init__(client, limit, prefix=prefix, now_ms=now_ms)
        self._window_ms = window * 1000

    def hit(self, key: str, cost: int = 1) -> Decision:
        validate_cost(cost)
        return self._run(key, [self._limit, repr(self._window_ms), cost, self._now_arg()])


class RedisSlidingWindow(_RedisLimiter):
    script_name = "sliding_window"
    key_tag = "sw"

    def __init__(
        self,
        client: redis.Redis,
        limit: int,
        window: float,
        *,
        prefix: str = "rl",
        now_ms: NowMs | None = None,
    ) -> None:
        if window <= 0:
            raise ValueError("window must be > 0 seconds")
        super().__init__(client, limit, prefix=prefix, now_ms=now_ms)
        self._window_ms = window * 1000

    def hit(self, key: str, cost: int = 1) -> Decision:
        validate_cost(cost)
        args = [self._limit, repr(self._window_ms), cost, self._now_arg(), uuid.uuid4().hex]
        return self._run(key, args)


def build_redis_limiter(
    algorithm: Algorithm | str,
    spec: LimitSpec,
    client: redis.Redis,
    *,
    prefix: str = "rl",
    now_ms: NowMs | None = None,
) -> RateLimiter:
    """The Redis twin of ``ratelimit.limits.build_limiter``."""
    match Algorithm(algorithm):
        case Algorithm.FIXED_WINDOW:
            return RedisFixedWindow(
                client, spec.requests, spec.period, prefix=prefix, now_ms=now_ms
            )
        case Algorithm.SLIDING_WINDOW:
            return RedisSlidingWindow(
                client, spec.requests, spec.period, prefix=prefix, now_ms=now_ms
            )
        case Algorithm.TOKEN_BUCKET:
            return RedisTokenBucket(client, spec.requests, spec.rate, prefix=prefix, now_ms=now_ms)
        case Algorithm.COUNTER:
            raise ValueError("the naive counter is a teaching baseline; it has no Redis version")


def connect(url: str) -> redis.Redis:
    """A client tuned for a limiter on the request path: fail fast, never hang.

    A rate limiter must not become the slowest part of a request. Short timeouts plus
    fail-open in the service mean a Redis outage degrades to "no limiting", not to
    "every request hangs".
    """
    return redis.Redis.from_url(
        url,
        socket_timeout=0.25,
        socket_connect_timeout=0.25,
        health_check_interval=30,
    )
