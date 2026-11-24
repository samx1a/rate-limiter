import math

import pytest
import redis

from ratelimit import FakeClock
from ratelimit.redis_limiters import RedisFixedWindow, RedisSlidingWindow

pytestmark = pytest.mark.redis

START = 1_700_000_000.0  # a multiple of 10, so window edges land on round numbers


def clock_ms(clock: FakeClock) -> float:
    return clock.now() * 1000


def fixed(
    client: redis.Redis, prefix: str, limit: int, window: float
) -> tuple[RedisFixedWindow, FakeClock]:
    clock = FakeClock(START)
    return RedisFixedWindow(
        client, limit, window, prefix=prefix, now_ms=lambda: clock_ms(clock)
    ), clock


def sliding(
    client: redis.Redis, prefix: str, limit: int, window: float
) -> tuple[RedisSlidingWindow, FakeClock]:
    clock = FakeClock(START)
    return RedisSlidingWindow(
        client, limit, window, prefix=prefix, now_ms=lambda: clock_ms(clock)
    ), clock


# ---- fixed window ---------------------------------------------------------------


def test_fixed_allows_limit_per_window(redis_client: redis.Redis, prefix: str) -> None:
    limiter, clock = fixed(redis_client, prefix, 3, 10)
    assert [limiter.hit("k").allowed for _ in range(4)] == [True, True, True, False]
    clock.advance(10)
    assert limiter.hit("k").allowed


def test_fixed_edge_burst_gets_twice_the_limit(redis_client: redis.Redis, prefix: str) -> None:
    limiter, clock = fixed(redis_client, prefix, 5, 10)
    clock.set(START + 9.9)
    first = [limiter.hit("k").allowed for _ in range(5)]
    clock.set(START + 10)
    second = [limiter.hit("k").allowed for _ in range(5)]
    assert all(first) and all(second)


def test_fixed_reports_window_end(redis_client: redis.Redis, prefix: str) -> None:
    limiter, clock = fixed(redis_client, prefix, 1, 10)
    clock.set(START + 7.5)
    limiter.hit("k")
    blocked = limiter.hit("k")
    assert not blocked.allowed
    assert blocked.retry_after == pytest.approx(2.5)
    assert blocked.reset_after == pytest.approx(2.5)
    assert limiter.hit("k", cost=2).retry_after == math.inf


def test_fixed_blocked_requests_are_free_and_keys_expire(
    redis_client: redis.Redis, prefix: str
) -> None:
    limiter, _ = fixed(redis_client, prefix, 4, 10)
    assert limiter.hit("k", cost=3).remaining == 1
    assert not limiter.hit("k", cost=2).allowed
    assert limiter.hit("k").allowed
    assert 0 < redis_client.pttl(limiter.redis_key("k")) <= 11_000


# ---- sliding window -------------------------------------------------------------


def test_sliding_blocks_the_edge_burst(redis_client: redis.Redis, prefix: str) -> None:
    limiter, clock = sliding(redis_client, prefix, 5, 10)
    clock.set(START + 9)
    assert all(limiter.hit("k").allowed for _ in range(5))
    clock.set(START + 10)
    assert not any(limiter.hit("k").allowed for _ in range(5))
    clock.set(START + 19)
    assert all(limiter.hit("k").allowed for _ in range(5))


def test_sliding_same_millisecond_requests_all_count(
    redis_client: redis.Redis, prefix: str
) -> None:
    """Members are unique, so requests at the same instant are not merged."""
    limiter, _ = sliding(redis_client, prefix, 50, 10)
    assert sum(limiter.hit("k").allowed for _ in range(60)) == 50
    assert redis_client.zcard(limiter.redis_key("k")) == 50


def test_sliding_retry_after_with_cost(redis_client: redis.Redis, prefix: str) -> None:
    limiter, clock = sliding(redis_client, prefix, 3, 10)
    for t in (0, 1, 2):
        clock.set(START + t)
        limiter.hit("k")
    clock.set(START + 5)
    blocked = limiter.hit("k", cost=2)  # waits for the t=0 and t=1 entries
    assert not blocked.allowed
    assert blocked.retry_after == pytest.approx(6)
    assert blocked.reset_after == pytest.approx(7)
    assert limiter.hit("k", cost=4).retry_after == math.inf


def test_sliding_entries_expire_one_by_one(redis_client: redis.Redis, prefix: str) -> None:
    limiter, clock = sliding(redis_client, prefix, 2, 10)
    limiter.hit("k")
    clock.set(START + 4)
    limiter.hit("k")
    clock.set(START + 10)
    assert limiter.hit("k").allowed
    assert not limiter.hit("k").allowed


def test_sliding_key_ttl_is_one_window(redis_client: redis.Redis, prefix: str) -> None:
    limiter, _ = sliding(redis_client, prefix, 2, 10)
    limiter.hit("k")
    assert 9_000 < redis_client.pttl(limiter.redis_key("k")) <= 10_000


def test_server_clock_mode(redis_client: redis.Redis, prefix: str) -> None:
    for cls in (RedisFixedWindow, RedisSlidingWindow):
        limiter = cls(redis_client, 2, 60, prefix=prefix)
        assert [limiter.hit("k").allowed for _ in range(3)] == [True, True, False]


def test_fixed_clock_stepping_back_does_not_reset_the_count(
    redis_client: redis.Redis, prefix: str
) -> None:
    """Redis TIME can jump back (failover to a replica, an NTP step). Going back across
    a window edge must not hand out a fresh window, or the clock jump leaks requests."""
    now = [(START + 10.5) * 1000]
    limiter = RedisFixedWindow(redis_client, 3, 10, prefix=prefix, now_ms=lambda: now[0])
    assert all(limiter.hit("k").allowed for _ in range(3))

    now[0] = (START + 9.5) * 1000  # one second back, into the previous window
    blocked = limiter.hit("k")
    assert not blocked.allowed
    assert blocked.retry_after == pytest.approx(10.5)  # until the stored window ends

    now[0] = (START + 20) * 1000  # the stored window really is over now
    assert limiter.hit("k").allowed


def test_sliding_clock_stepping_back_keeps_entries(redis_client: redis.Redis, prefix: str) -> None:
    now = [(START + 10.5) * 1000]
    limiter = RedisSlidingWindow(redis_client, 3, 10, prefix=prefix, now_ms=lambda: now[0])
    assert all(limiter.hit("k").allowed for _ in range(3))
    now[0] = (START + 9.5) * 1000
    blocked = limiter.hit("k")
    assert not blocked.allowed
    assert blocked.retry_after == pytest.approx(11)  # entries at 10.5 expire at 20.5
