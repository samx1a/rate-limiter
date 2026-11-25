import math

import pytest

from ratelimit import FakeClock
from ratelimit.algorithms.sliding_window import SlidingWindowLog


def make(limit: int = 3, window: float = 10.0) -> tuple[SlidingWindowLog, FakeClock]:
    clock = FakeClock()
    return SlidingWindowLog(limit, window, clock), clock


def test_allows_limit_within_any_window() -> None:
    limiter, _ = make(limit=3)
    assert [limiter.hit("k").allowed for _ in range(4)] == [True, True, True, False]


def test_edge_burst_is_blocked() -> None:
    """The same burst that gets 2x through a fixed window is capped here."""
    limit = 5
    limiter, clock = make(limit=limit, window=10)
    clock.set(9)
    assert all(limiter.hit("k").allowed for _ in range(limit))
    clock.set(10)  # a fixed window would have reset here
    assert not any(limiter.hit("k").allowed for _ in range(limit))
    clock.set(19)  # 10s after the burst, the old entries have slid out
    assert all(limiter.hit("k").allowed for _ in range(limit))


def test_entries_expire_one_by_one() -> None:
    limiter, clock = make(limit=2, window=10)
    limiter.hit("k")  # t=0
    clock.set(4)
    limiter.hit("k")  # t=4
    clock.set(9)
    assert not limiter.hit("k").allowed
    clock.set(10)  # entry from t=0 expires exactly now
    assert limiter.hit("k").allowed
    assert not limiter.hit("k").allowed  # entry from t=4 still counts until t=14


def test_retry_after_points_at_the_oldest_entry() -> None:
    limiter, clock = make(limit=2, window=10)
    limiter.hit("k")  # t=0
    clock.set(3)
    limiter.hit("k")  # t=3
    clock.set(5)
    blocked = limiter.hit("k")
    assert blocked.retry_after == pytest.approx(5)  # t=0 entry expires at t=10
    assert blocked.reset_after == pytest.approx(8)  # t=3 entry expires at t=13
    assert blocked.remaining == 0


def test_retry_after_with_cost_needs_several_entries_to_expire() -> None:
    limiter, clock = make(limit=3, window=10)
    for t in (0, 1, 2):
        clock.set(t)
        limiter.hit("k")
    clock.set(5)
    blocked = limiter.hit("k", cost=2)  # must wait for the t=0 and t=1 entries
    assert blocked.retry_after == pytest.approx(6)


def test_cost_above_limit_can_never_succeed() -> None:
    limiter, _ = make(limit=2)
    blocked = limiter.hit("k", cost=3)
    assert not blocked.allowed and blocked.retry_after == math.inf


def test_memory_per_key_is_bounded_by_limit() -> None:
    limiter, clock = make(limit=5, window=10)
    for _ in range(1000):
        limiter.hit("k")
        clock.advance(0.001)
    assert len(limiter._state["k"].entries) <= 5


def test_waiting_exactly_reset_after_frees_the_log_despite_float_rounding() -> None:
    """Found by the property tests: newest + window - now, added back onto now, can
    land 2e-15 s short of newest + window, so the entry never looked expired."""
    limiter, clock = make(limit=5, window=10)
    last = None
    for gap in (0.0, 0.0, 0.0, 1.0, 0.001, 0.001):
        clock.advance(gap)
        last = limiter.hit("k")
    assert last is not None
    clock.advance(last.reset_after)
    assert limiter.hit("k", cost=5).allowed
