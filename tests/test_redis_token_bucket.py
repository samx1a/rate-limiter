import math

import pytest
import redis

from ratelimit import FakeClock
from ratelimit.redis_limiters import RedisTokenBucket

pytestmark = pytest.mark.redis


def make(
    client: redis.Redis, prefix: str, capacity: int = 5, rate: float = 1.0
) -> tuple[RedisTokenBucket, FakeClock]:
    clock = FakeClock(1_700_000_000.0)
    bucket = RedisTokenBucket(
        client, capacity, rate, prefix=prefix, now_ms=lambda: clock.now() * 1000
    )
    return bucket, clock


def test_burst_then_block(redis_client: redis.Redis, prefix: str) -> None:
    bucket, _ = make(redis_client, prefix, capacity=5)
    results = [bucket.hit("k") for _ in range(6)]
    assert [r.allowed for r in results] == [True] * 5 + [False]
    assert [r.remaining for r in results] == [4, 3, 2, 1, 0, 0]


def test_lazy_refill_and_cap(redis_client: redis.Redis, prefix: str) -> None:
    bucket, clock = make(redis_client, prefix, capacity=3, rate=2.0)
    for _ in range(3):
        bucket.hit("k")
    clock.advance(1.0)
    assert [bucket.hit("k").allowed for _ in range(3)] == [True, True, False]
    clock.advance(1_000)
    assert [bucket.hit("k").allowed for _ in range(4)] == [True, True, True, False]


def test_float_drift_does_not_block(redis_client: redis.Redis, prefix: str) -> None:
    bucket, clock = make(redis_client, prefix, capacity=1, rate=0.1)
    bucket.hit("k")
    for _ in range(9):
        clock.advance(1.0)
        assert not bucket.hit("k").allowed
    clock.advance(1.0)
    assert bucket.hit("k").allowed


def test_retry_and_reset_times(redis_client: redis.Redis, prefix: str) -> None:
    bucket, _ = make(redis_client, prefix, capacity=4, rate=2.0)
    for _ in range(4):
        bucket.hit("k")
    blocked = bucket.hit("k", cost=3)
    assert not blocked.allowed
    assert blocked.retry_after == pytest.approx(1.5)
    assert blocked.reset_after == pytest.approx(2.0)


def test_cost_above_capacity_never_succeeds(redis_client: redis.Redis, prefix: str) -> None:
    bucket, _ = make(redis_client, prefix, capacity=2)
    assert bucket.hit("k", cost=3).retry_after == math.inf


def test_state_is_shared_between_limiter_objects(redis_client: redis.Redis, prefix: str) -> None:
    """Two app servers = two limiter objects on one Redis: they share the bucket."""
    clock = FakeClock(1_700_000_000.0)
    server_a = RedisTokenBucket(
        redis_client, 2, 1.0, prefix=prefix, now_ms=lambda: clock.now() * 1000
    )
    server_b = RedisTokenBucket(
        redis_client, 2, 1.0, prefix=prefix, now_ms=lambda: clock.now() * 1000
    )
    assert server_a.hit("k").allowed
    assert server_b.hit("k").allowed
    assert not server_a.hit("k").allowed


def test_keys_expire_once_the_bucket_would_be_full(redis_client: redis.Redis, prefix: str) -> None:
    bucket, _ = make(redis_client, prefix, capacity=10, rate=2.0)
    bucket.hit("k", cost=4)  # 4 tokens to refill at 2/s = 2s, plus 1s margin
    ttl_ms = redis_client.pttl(bucket.redis_key("k"))
    # Upper bound is exact; the lower bound leaves room for the network round trip
    # between the script and PTTL when Redis is remote.
    assert 2_000 < ttl_ms <= 3_000


def test_uses_the_redis_server_clock_by_default(redis_client: redis.Redis, prefix: str) -> None:
    bucket = RedisTokenBucket(redis_client, 2, 1.0, prefix=prefix)
    assert [bucket.hit("k").allowed for _ in range(3)] == [True, True, False]


def test_survives_a_script_cache_flush(redis_client: redis.Redis, prefix: str) -> None:
    bucket, _ = make(redis_client, prefix, capacity=2)
    bucket.hit("k")
    redis_client.script_flush()  # e.g. a Redis restart or failover
    assert bucket.hit("k").allowed


def test_clock_stepping_back_neither_refills_nor_breaks(
    redis_client: redis.Redis, prefix: str
) -> None:
    now = [1_700_000_010_000.0]
    bucket = RedisTokenBucket(redis_client, 3, 1.0, prefix=prefix, now_ms=lambda: now[0])
    assert all(bucket.hit("k").allowed for _ in range(3))
    now[0] -= 5_000  # Redis TIME jumped 5 seconds back
    assert not bucket.hit("k").allowed
    now[0] += 6_000  # 1 second past the last real refill: exactly 1 token back
    assert bucket.hit("k").allowed
    assert not bucket.hit("k").allowed
