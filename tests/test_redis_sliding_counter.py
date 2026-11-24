import math

import pytest
import redis

from ratelimit import FakeClock
from ratelimit.algorithms.sliding_counter import SlidingWindowCounter
from ratelimit.redis_limiters import RedisSlidingCounter

pytestmark = pytest.mark.redis

START = 1_700_000_000.0  # a multiple of 10, so window edges land on round numbers


def make(
    client: redis.Redis, prefix: str, limit: int = 10, window: float = 10.0
) -> tuple[RedisSlidingCounter, FakeClock]:
    clock = FakeClock(START)
    limiter = RedisSlidingCounter(
        client, limit, window, prefix=prefix, now_ms=lambda: clock.now() * 1000
    )
    return limiter, clock


def test_weights_the_previous_window(redis_client: redis.Redis, prefix: str) -> None:
    limiter, clock = make(redis_client, prefix)
    clock.set(START + 5)
    for _ in range(8):
        limiter.hit("k")
    clock.set(START + 17.5)  # estimate = 8 * 0.25 = 2
    decisions = [limiter.hit("k") for _ in range(9)]
    assert [d.allowed for d in decisions] == [True] * 8 + [False]
    assert decisions[0].remaining == 7


def test_retry_after_matches_in_memory(redis_client: redis.Redis, prefix: str) -> None:
    redis_limiter, clock = make(redis_client, prefix, limit=4)
    memory = SlidingWindowCounter(4, 10, clock)
    clock.set(START + 2)
    for _ in range(4):
        assert redis_limiter.hit("k").allowed and memory.hit("k").allowed
    r, m = redis_limiter.hit("k"), memory.hit("k")
    assert not r.allowed and not m.allowed
    assert r.retry_after == pytest.approx(m.retry_after) == pytest.approx(10.5)
    assert r.reset_after == pytest.approx(m.reset_after)


def test_key_lives_until_the_end_of_the_next_window(redis_client: redis.Redis, prefix: str) -> None:
    limiter, clock = make(redis_client, prefix)
    clock.set(START + 3)
    limiter.hit("k")
    ttl = redis_client.pttl(limiter.redis_key("k"))
    assert 17_000 < ttl <= 18_000


def test_clock_stepping_back_keeps_the_count(redis_client: redis.Redis, prefix: str) -> None:
    now = [(START + 10.5) * 1000]
    limiter = RedisSlidingCounter(redis_client, 3, 10, prefix=prefix, now_ms=lambda: now[0])
    assert all(limiter.hit("k").allowed for _ in range(3))
    now[0] = (START + 9.5) * 1000
    assert not limiter.hit("k").allowed


def test_cost_above_limit_never_fits(redis_client: redis.Redis, prefix: str) -> None:
    limiter, _ = make(redis_client, prefix, limit=3)
    assert limiter.hit("k", cost=4).retry_after == math.inf


def test_server_clock_mode(redis_client: redis.Redis, prefix: str) -> None:
    limiter = RedisSlidingCounter(redis_client, 2, 60, prefix=prefix)
    assert [limiter.hit("k").allowed for _ in range(3)] == [True, True, False]
