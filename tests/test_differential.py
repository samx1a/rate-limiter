"""Differential test: the in-memory classes and the Redis scripts must agree.

The contract tests check both backends against a handful of fixed scenarios. This
runs the same random traffic through the Python and the Lua version of each algorithm,
side by side on one fake clock, and compares every decision. Two implementations of
the same math in two languages is exactly where small differences creep in.
"""

from __future__ import annotations

import math
import uuid

import pytest
import redis
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ratelimit import Decision, FakeClock
from ratelimit.limits import Algorithm, LimitSpec, build_limiter
from ratelimit.redis_limiters import build_redis_limiter
from tests.test_properties import traffic

pytestmark = pytest.mark.redis

START = 1_700_000_000.0
SPEC = LimitSpec(5, 10)
ALGORITHMS = [a for a in Algorithm if a is not Algorithm.COUNTER]


def compare(algorithm: Algorithm, step: int, a: Decision, b: Decision) -> Decision:
    where = (algorithm, step, a, b)
    assert (a.allowed, a.remaining) == (b.allowed, b.remaining), where
    for x, y in ((a.retry_after, b.retry_after), (a.reset_after, b.reset_after)):
        assert x == y or math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-6), where
    return a


@settings(
    max_examples=60,
    deadline=None,  # each example makes up to 160 Redis calls
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(algorithm=st.sampled_from(ALGORITHMS), requests=traffic)
def test_memory_and_redis_make_the_same_decisions(
    redis_client: redis.Redis, algorithm: Algorithm, requests: list[tuple[float, int]]
) -> None:
    clock = FakeClock(START)
    prefix = f"diff-{uuid.uuid4().hex[:12]}"
    memory = build_limiter(algorithm, SPEC, clock)
    remote = build_redis_limiter(
        algorithm, SPEC, redis_client, prefix=prefix, now_ms=lambda: clock.now() * 1000
    )
    try:
        for step, (gap, cost) in enumerate(requests):
            clock.advance(gap)
            a = compare(algorithm, step, memory.hit("k", cost), remote.hit("k", cost))
            if not a.allowed and math.isfinite(a.retry_after):
                # Retry exactly when told to. That instant is the knife edge where the
                # two implementations' rounding would show.
                clock.advance(a.retry_after)
                compare(algorithm, step, memory.hit("k", cost), remote.hit("k", cost))
    finally:
        keys = list(redis_client.scan_iter(f"{prefix}:*"))
        if keys:
            redis_client.delete(*keys)
