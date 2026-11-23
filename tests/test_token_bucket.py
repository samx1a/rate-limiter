import math
import threading

import pytest

from ratelimit import FakeClock
from ratelimit.algorithms.token_bucket import TokenBucket


def make(capacity: int = 5, rate: float = 1.0) -> tuple[TokenBucket, FakeClock]:
    clock = FakeClock()
    return TokenBucket(capacity, rate, clock), clock


def test_starts_full_and_allows_a_burst_of_capacity() -> None:
    bucket, _ = make(capacity=5)
    results = [bucket.hit("k") for _ in range(6)]
    assert [r.allowed for r in results] == [True] * 5 + [False]
    assert [r.remaining for r in results] == [4, 3, 2, 1, 0, 0]


def test_refills_lazily_with_elapsed_time() -> None:
    bucket, clock = make(capacity=5, rate=2.0)  # 2 tokens per second
    for _ in range(5):
        bucket.hit("k")
    clock.advance(1.0)
    assert [bucket.hit("k").allowed for _ in range(3)] == [True, True, False]


def test_fractional_tokens_accumulate() -> None:
    bucket, clock = make(capacity=1, rate=0.5)
    bucket.hit("k")
    clock.advance(1.0)  # 0.5 tokens: not enough
    assert not bucket.hit("k").allowed
    clock.advance(1.0)  # 1.0 token
    assert bucket.hit("k").allowed


def test_never_refills_past_capacity() -> None:
    bucket, clock = make(capacity=3, rate=1.0)
    bucket.hit("k")
    clock.advance(1_000)
    assert [bucket.hit("k").allowed for _ in range(4)] == [True, True, True, False]


def test_floating_point_refill_is_not_cut_short() -> None:
    bucket, clock = make(capacity=1, rate=0.1)
    bucket.hit("k")
    # Each blocked hit stores the refilled amount, so tokens are summed 0.1 at a
    # time: 0.1 + 0.1 + ... (10 times) == 0.9999999999999999 in floating point.
    for _ in range(9):
        clock.advance(1.0)
        assert not bucket.hit("k").allowed
    clock.advance(1.0)
    assert bucket.hit("k").allowed


def test_retry_after_and_reset_after() -> None:
    bucket, clock = make(capacity=4, rate=2.0)
    for _ in range(4):
        bucket.hit("k")
    blocked = bucket.hit("k", cost=3)
    assert not blocked.allowed
    assert blocked.retry_after == pytest.approx(1.5)  # need 3 tokens at 2/s
    assert blocked.reset_after == pytest.approx(2.0)  # 4 tokens to full at 2/s
    clock.advance(1.5)
    assert bucket.hit("k", cost=3).allowed


def test_cost_above_capacity_can_never_succeed() -> None:
    bucket, _ = make(capacity=2)
    blocked = bucket.hit("k", cost=3)
    assert not blocked.allowed and blocked.retry_after == math.inf


def test_blocked_requests_spend_nothing() -> None:
    bucket, clock = make(capacity=2, rate=1.0)
    bucket.hit("k", cost=2)
    for _ in range(10):
        assert not bucket.hit("k").allowed
    clock.advance(1.0)
    assert bucket.hit("k").allowed


def test_long_run_rate_is_capped_by_refill_rate() -> None:
    capacity, rate, seconds = 10, 5.0, 60
    bucket, clock = make(capacity=capacity, rate=rate)
    admitted = 0
    for _ in range(seconds * 100):  # a client trying 100 req/s for a minute
        admitted += bucket.hit("k").allowed
        clock.advance(0.01)
    assert admitted == pytest.approx(capacity + rate * seconds, abs=1)


def test_uses_no_background_threads() -> None:
    before = threading.active_count()
    bucket, clock = make()
    for _ in range(100):
        bucket.hit("k")
        clock.advance(0.5)
    assert threading.active_count() == before


@pytest.mark.parametrize(("capacity", "rate"), [(0, 1.0), (1, 0.0), (1, -2.0)])
def test_rejects_bad_config(capacity: int, rate: float) -> None:
    with pytest.raises(ValueError):
        TokenBucket(capacity, rate)
