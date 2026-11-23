import pytest

from ratelimit import FakeClock
from ratelimit.algorithms.fixed_window import FixedWindow


def make(limit: int = 3, window: float = 10.0, start: float = 0.0) -> tuple[FixedWindow, FakeClock]:
    clock = FakeClock(start)
    return FixedWindow(limit, window, clock), clock


def test_allows_limit_per_window_then_blocks() -> None:
    limiter, _ = make(limit=3)
    assert [limiter.hit("k").allowed for _ in range(4)] == [True, True, True, False]


def test_count_starts_over_in_the_next_window() -> None:
    limiter, clock = make(limit=1, window=10)
    assert limiter.hit("k").allowed
    assert not limiter.hit("k").allowed
    clock.set(10.0)  # exactly on the edge belongs to the new window
    assert limiter.hit("k").allowed


def test_reports_time_until_the_window_ends() -> None:
    limiter, clock = make(limit=1, window=10)
    clock.set(7.5)
    allowed = limiter.hit("k")
    assert allowed.reset_after == pytest.approx(2.5)
    blocked = limiter.hit("k")
    assert not blocked.allowed
    assert blocked.retry_after == pytest.approx(2.5)
    assert blocked.remaining == 0


def test_skipping_several_windows_resets_the_count() -> None:
    limiter, clock = make(limit=1, window=1)
    limiter.hit("k")
    clock.advance(5.3)
    assert limiter.hit("k").allowed


def test_edge_burst_lets_through_twice_the_limit() -> None:
    """The known weakness: 2 x limit requests in a tiny slice of time."""
    limit = 5
    limiter, clock = make(limit=limit, window=10)

    clock.set(9.9)  # last 0.1s of window [0, 10)
    first = [limiter.hit("k").allowed for _ in range(limit)]
    clock.set(10.0)  # first instant of window [10, 20)
    second = [limiter.hit("k").allowed for _ in range(limit)]

    assert all(first) and all(second)
    admitted_in_a_tenth_of_a_second = sum(first) + sum(second)
    assert admitted_in_a_tenth_of_a_second == 2 * limit


def test_blocked_requests_do_not_count_and_cost_is_respected() -> None:
    limiter, _ = make(limit=4)
    assert limiter.hit("k", cost=3).remaining == 1
    assert not limiter.hit("k", cost=2).allowed
    assert limiter.hit("k", cost=1).allowed


@pytest.mark.parametrize(("limit", "window"), [(0, 1.0), (1, 0.0), (1, -1.0)])
def test_rejects_bad_config(limit: int, window: float) -> None:
    with pytest.raises(ValueError):
        FixedWindow(limit, window)
