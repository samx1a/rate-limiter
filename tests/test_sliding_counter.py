import math

import pytest

from ratelimit import FakeClock
from ratelimit.algorithms.sliding_counter import SlidingWindowCounter


def make(limit: int = 10, window: float = 10.0) -> tuple[SlidingWindowCounter, FakeClock]:
    clock = FakeClock()
    return SlidingWindowCounter(limit, window, clock), clock


def test_allows_limit_in_one_window() -> None:
    limiter, _ = make(limit=3)
    assert [limiter.hit("k").allowed for _ in range(4)] == [True, True, True, False]


def test_previous_window_is_weighted_by_how_much_is_still_in_view() -> None:
    limiter, clock = make(limit=10, window=10)
    clock.set(5)
    for _ in range(8):
        limiter.hit("k")  # 8 in window [0, 10)
    clock.set(17.5)  # 25% of the previous window still in view: estimate = 8 * 0.25 = 2
    decisions = [limiter.hit("k") for _ in range(9)]
    assert [d.allowed for d in decisions] == [True] * 8 + [False]
    assert decisions[0].remaining == 7  # 10 - (2 + 1)


def test_edge_burst_is_mostly_blocked() -> None:
    """Fixed window lets 2x the limit through at an edge. Here the previous window
    still counts almost fully right after the edge."""
    limiter, clock = make(limit=5, window=10)
    clock.set(9.8)
    assert all(limiter.hit("k").allowed for _ in range(5))
    clock.set(10.0)
    assert not any(limiter.hit("k").allowed for _ in range(5))


def test_counts_reset_after_two_idle_windows() -> None:
    limiter, clock = make(limit=2, window=10)
    limiter.hit("k")
    limiter.hit("k")
    clock.set(25)  # window 2: window 0 is no longer "previous"
    assert limiter.hit("k").allowed
    assert limiter.hit("k").allowed


def test_blocked_requests_are_not_counted() -> None:
    limiter, _ = make(limit=4)
    limiter.hit("k", cost=3)
    assert not limiter.hit("k", cost=2).allowed
    assert limiter.hit("k", cost=1).allowed


def test_retry_after_within_the_same_window() -> None:
    limiter, clock = make(limit=10, window=10)
    for _ in range(10):
        limiter.hit("k")  # window 0
    clock.set(12)  # estimate = 10 * 0.8 = 8
    assert limiter.hit("k", cost=2).allowed  # 8 + 2 = 10
    blocked = limiter.hit("k")
    assert not blocked.allowed
    # current=2, so the previous window must fall to 10-2-1 = 7: weight 0.7 at t=13.
    assert blocked.retry_after == pytest.approx(1.0)
    clock.advance(blocked.retry_after)
    assert limiter.hit("k").allowed


def test_retry_after_waits_for_the_rollover_when_current_is_full() -> None:
    limiter, clock = make(limit=4, window=10)
    clock.set(2)
    for _ in range(4):
        limiter.hit("k")
    blocked = limiter.hit("k")
    # The current window alone is full. After the rollover at t=10, the 4 become
    # "previous" and must fade to 3: weight 0.75, i.e. 2.5s into window 1.
    assert blocked.retry_after == pytest.approx(10.5)
    clock.advance(blocked.retry_after)
    assert limiter.hit("k").allowed
    assert not limiter.hit("k").allowed


def test_reset_after_covers_the_next_window_too() -> None:
    limiter, clock = make(limit=4, window=10)
    clock.set(3)
    assert limiter.hit("k").reset_after == pytest.approx(17)  # in view until t=20
    clock.set(20)
    d = limiter.hit("other")
    assert d.reset_after == pytest.approx(20)


def test_cost_above_limit_never_fits() -> None:
    limiter, _ = make(limit=3)
    blocked = limiter.hit("k", cost=4)
    assert not blocked.allowed and blocked.retry_after == math.inf


def test_memory_per_key_does_not_grow_with_the_limit() -> None:
    limiter, _ = make(limit=100_000, window=3600)
    for _ in range(5_000):
        limiter.hit("k")
    assert limiter._state == {"k": (0, 5_000, 0)}


@pytest.mark.parametrize(("limit", "window"), [(0, 1.0), (1, 0.0)])
def test_rejects_bad_config(limit: int, window: float) -> None:
    with pytest.raises(ValueError):
        SlidingWindowCounter(limit, window)
