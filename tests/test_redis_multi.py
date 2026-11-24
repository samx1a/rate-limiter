"""hit_many: several limits in one round trip, all or nothing."""

import random

import pytest
import redis

from ratelimit import FakeClock
from ratelimit.limits import Algorithm, LimitSpec
from ratelimit.redis_limiters import (
    RedisFixedWindow,
    RedisTokenBucket,
    build_redis_limiter,
    hit_many,
)

pytestmark = pytest.mark.redis

START = 1_700_000_000.0


def pair(
    client: redis.Redis, prefix: str, ip_limit: int, user_limit: int
) -> tuple[RedisTokenBucket, RedisTokenBucket, FakeClock]:
    clock = FakeClock(START)

    def now() -> float:
        return clock.now() * 1000

    ip = RedisTokenBucket(client, ip_limit, 0.1, prefix=f"{prefix}:ip", now_ms=now)
    user = RedisTokenBucket(client, user_limit, 0.1, prefix=f"{prefix}:user", now_ms=now)
    return ip, user, clock


def test_spends_from_every_key_when_all_allow(redis_client: redis.Redis, prefix: str) -> None:
    ip, user, _ = pair(redis_client, prefix, 5, 3)
    decisions = hit_many([(ip, "1.2.3.4", 1), (user, "alice", 1)])
    assert [(d.allowed, d.remaining) for d in decisions] == [(True, 4), (True, 2)]
    assert ip.hit("1.2.3.4").remaining == 3  # the first call really spent


def test_a_block_on_one_key_spends_nothing_anywhere(redis_client: redis.Redis, prefix: str) -> None:
    ip, user, _ = pair(redis_client, prefix, 5, 1)
    user.hit("alice")  # alice is out
    decisions = hit_many([(ip, "1.2.3.4", 1), (user, "alice", 1)])
    assert [d.allowed for d in decisions] == [True, False]
    assert ip.hit("1.2.3.4").remaining == 4  # untouched by the blocked request


def test_stops_at_the_first_block(redis_client: redis.Redis, prefix: str) -> None:
    ip, user, _ = pair(redis_client, prefix, 1, 5)
    ip.hit("1.2.3.4")
    decisions = hit_many([(ip, "1.2.3.4", 1), (user, "alice", 1)])
    assert len(decisions) == 1 and not decisions[0].allowed
    assert user.hit("alice").remaining == 4  # never checked, never spent


@pytest.mark.parametrize("algorithm", [a for a in Algorithm if a is not Algorithm.COUNTER])
def test_one_key_matches_a_plain_hit(
    redis_client: redis.Redis, prefix: str, algorithm: Algorithm
) -> None:
    clock = FakeClock(START)
    spec = LimitSpec(4, 10)

    def now() -> float:
        return clock.now() * 1000

    plain = build_redis_limiter(algorithm, spec, redis_client, prefix=f"{prefix}:a", now_ms=now)
    multi = build_redis_limiter(algorithm, spec, redis_client, prefix=f"{prefix}:b", now_ms=now)
    rng = random.Random(1)
    for _ in range(200):
        clock.advance(rng.choice([0, 0.3, 1, 4]))
        cost = rng.choice([1, 1, 2, 5])
        assert hit_many([(multi, "k", cost)]) == [plain.hit("k", cost)]


def test_rejects_mixed_algorithms(redis_client: redis.Redis, prefix: str) -> None:
    bucket = RedisTokenBucket(redis_client, 5, 1.0, prefix=prefix)
    window = RedisFixedWindow(redis_client, 5, 10, prefix=prefix)
    with pytest.raises(TypeError):
        hit_many([(bucket, "a", 1), (window, "b", 1)])
    assert hit_many([]) == []
