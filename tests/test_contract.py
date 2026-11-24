"""One behaviour contract, checked against every algorithm on both backends.

If the Redis scripts and the in-memory classes ever disagree, these tests say so.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

from ratelimit import FakeClock, RateLimiter
from ratelimit.limits import LimitSpec, build_limiter
from ratelimit.redis_limiters import build_redis_limiter

LIMIT, PERIOD = 4, 10.0
START = 1_700_000_000.0


@dataclass
class Subject:
    limiter: RateLimiter
    clock: FakeClock


Factory = Callable[[str], Subject]


@pytest.fixture(params=["fixed_window", "sliding_window", "sliding_counter", "token_bucket"])
def algorithm(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.redis)])
def make(request: pytest.FixtureRequest, algorithm: str) -> Iterator[Subject]:
    clock = FakeClock(START)
    spec = LimitSpec(LIMIT, PERIOD)
    if request.param == "memory":
        yield Subject(build_limiter(algorithm, spec, clock), clock)
    else:
        client = request.getfixturevalue("redis_client")
        prefix = request.getfixturevalue("prefix")
        limiter = build_redis_limiter(
            algorithm, spec, client, prefix=prefix, now_ms=lambda: clock.now() * 1000
        )
        yield Subject(limiter, clock)


def test_admits_exactly_the_limit_in_a_burst(make: Subject) -> None:
    results = [make.limiter.hit("k") for _ in range(LIMIT + 2)]
    assert [r.allowed for r in results] == [True] * LIMIT + [False, False]
    assert [r.remaining for r in results] == [3, 2, 1, 0, 0, 0]
    assert all(r.limit == LIMIT for r in results)


def test_blocked_decisions_are_well_formed(make: Subject) -> None:
    for _ in range(LIMIT):
        make.limiter.hit("k")
    blocked = make.limiter.hit("k")
    assert not blocked.allowed
    # Most algorithms recover within one period. The sliding counter can take up to two:
    # this window's count stays (weighted) in view through the whole next window.
    assert 0 < blocked.retry_after <= 2 * PERIOD
    assert 0 < blocked.reset_after <= 2 * PERIOD


def test_blocked_requests_do_not_consume(make: Subject) -> None:
    make.limiter.hit("k", cost=3)
    assert not make.limiter.hit("k", cost=2).allowed
    assert make.limiter.hit("k", cost=1).allowed


def test_full_limit_is_back_after_reset_after(make: Subject) -> None:
    """reset_after is a promise: wait that long and the whole limit is available."""
    last = [make.limiter.hit("k") for _ in range(LIMIT)][-1]
    make.clock.advance(last.reset_after)
    assert [make.limiter.hit("k").allowed for _ in range(LIMIT)] == [True] * LIMIT


def test_full_limit_is_back_after_one_period(make: Subject, algorithm: str) -> None:
    if algorithm == "sliding_counter":
        pytest.skip("estimates: a burst at a window start still counts one period later")
    for _ in range(LIMIT):
        make.limiter.hit("k")
    make.clock.advance(PERIOD)
    assert [make.limiter.hit("k").allowed for _ in range(LIMIT)] == [True] * LIMIT


def test_retry_after_is_honest(make: Subject) -> None:
    """Waiting exactly retry_after seconds is enough to get in."""
    for _ in range(LIMIT):
        make.limiter.hit("k")
    blocked = make.limiter.hit("k")
    make.clock.advance(blocked.retry_after)
    assert make.limiter.hit("k").allowed


def test_keys_are_independent(make: Subject) -> None:
    for _ in range(LIMIT):
        make.limiter.hit("a")
    assert make.limiter.hit("b").allowed


def test_cost_above_limit_never_fits(make: Subject) -> None:
    blocked = make.limiter.hit("k", cost=LIMIT + 1)
    assert not blocked.allowed and blocked.retry_after == math.inf
