"""Property-based tests: each algorithm's promise, checked on random traffic.

The example-based tests pin specific scenarios. These let Hypothesis invent thousands
of request patterns (bursts, gaps, odd costs, requests landing exactly on window
edges) and check the guarantee that matters for each algorithm still holds. When one
fails, Hypothesis shrinks it to the smallest traffic pattern that breaks it.
"""

from __future__ import annotations

from collections.abc import Callable

from hypothesis import given, settings
from hypothesis import strategies as st

from ratelimit import FakeClock, RateLimiter
from ratelimit.algorithms.counter import NaiveCounter
from ratelimit.algorithms.fixed_window import FixedWindow
from ratelimit.algorithms.sliding_counter import SlidingWindowCounter
from ratelimit.algorithms.sliding_window import SlidingWindowLog
from ratelimit.algorithms.token_bucket import TokenBucket

LIMIT = 5
WINDOW = 10.0
RATE = LIMIT / WINDOW

# Gaps between requests: often 0 (simultaneous), often tiny, sometimes long. Using
# multiples of 0.25 makes requests land exactly on window edges a lot, and gaps of
# "one window minus a hair" land just before an old request expires. Off-by-one and
# off-by-a-millisecond bugs live at both spots.
gaps = st.one_of(
    st.just(0.0),
    st.integers(0, 60).map(lambda n: n * 0.25),
    st.sampled_from([0.001, WINDOW - 0.001, WINDOW + 0.001]),
    st.floats(0, 2 * WINDOW, allow_nan=False),
)
traffic = st.lists(st.tuples(gaps, st.integers(1, LIMIT + 1)), max_size=80)

Admitted = list[tuple[float, int]]


def run(make: Callable[[FakeClock], RateLimiter], requests: list[tuple[float, int]]) -> Admitted:
    clock = FakeClock()
    limiter = make(clock)
    admitted: Admitted = []
    for gap, cost in requests:
        clock.advance(gap)
        decision = limiter.hit("k", cost)
        assert 0 <= decision.remaining <= LIMIT
        assert decision.allowed == (decision.retry_after == 0)
        if decision.allowed:
            admitted.append((clock.now(), cost))
    return admitted


def cost_in(admitted: Admitted, start: float, end: float) -> int:
    """Admitted cost with start <= t < end."""
    return sum(cost for t, cost in admitted if start <= t < end)


@given(traffic)
def test_counter_never_admits_more_than_the_limit_in_total(
    requests: list[tuple[float, int]],
) -> None:
    admitted = run(lambda _: NaiveCounter(LIMIT), requests)
    assert sum(cost for _, cost in admitted) <= LIMIT


@given(traffic)
def test_fixed_window_caps_each_calendar_window_but_not_each_span(
    requests: list[tuple[float, int]],
) -> None:
    admitted = run(lambda c: FixedWindow(LIMIT, WINDOW, c), requests)
    for t, _ in admitted:
        start = (t // WINDOW) * WINDOW
        assert cost_in(admitted, start, start + WINDOW) <= LIMIT
        # Any WINDOW-long span touches at most two calendar windows: the edge burst.
        assert cost_in(admitted, t, t + WINDOW) <= 2 * LIMIT


@given(traffic)
def test_sliding_log_caps_every_window_long_span(requests: list[tuple[float, int]]) -> None:
    admitted = run(lambda c: SlidingWindowLog(LIMIT, WINDOW, c), requests)
    for t, _ in admitted:
        assert cost_in(admitted, t, t + WINDOW) <= LIMIT


@given(traffic)
def test_sliding_counter_caps_calendar_windows_and_at_worst_doubles(
    requests: list[tuple[float, int]],
) -> None:
    admitted = run(lambda c: SlidingWindowCounter(LIMIT, WINDOW, c), requests)
    for t, _ in admitted:
        start = (t // WINDOW) * WINDOW
        assert cost_in(admitted, start, start + WINDOW) <= LIMIT  # estimate >= current
        assert cost_in(admitted, t, t + WINDOW) <= 2 * LIMIT


@given(traffic)
def test_token_bucket_never_beats_burst_plus_refill(requests: list[tuple[float, int]]) -> None:
    admitted = run(lambda c: TokenBucket(LIMIT, RATE, c), requests)
    for i, (start, _) in enumerate(admitted):
        for end, _ in admitted[i:]:
            spent = cost_in(admitted, start, end + 1e-12)
            assert spent <= LIMIT + RATE * (end - start) + 1e-6


ALL: dict[str, Callable[[FakeClock], RateLimiter]] = {
    "fixed_window": lambda c: FixedWindow(LIMIT, WINDOW, c),
    "sliding_window": lambda c: SlidingWindowLog(LIMIT, WINDOW, c),
    "sliding_counter": lambda c: SlidingWindowCounter(LIMIT, WINDOW, c),
    "token_bucket": lambda c: TokenBucket(LIMIT, RATE, c),
}


@settings(max_examples=150)
@given(st.sampled_from(sorted(ALL)), traffic, st.integers(1, LIMIT))
def test_waiting_retry_after_is_always_enough(
    name: str, requests: list[tuple[float, int]], cost: int
) -> None:
    """Whatever happened before, a blocked request that waits retry_after gets in."""
    clock = FakeClock()
    limiter = ALL[name](clock)
    for gap, c in requests:
        clock.advance(gap)
        limiter.hit("k", c)
    decision = limiter.hit("k", cost)
    if not decision.allowed:
        clock.advance(decision.retry_after)
        assert limiter.hit("k", cost).allowed


@settings(max_examples=150)
@given(st.sampled_from(sorted(ALL)), traffic)
def test_waiting_reset_after_brings_back_the_whole_limit(
    name: str, requests: list[tuple[float, int]]
) -> None:
    clock = FakeClock()
    limiter = ALL[name](clock)
    last = None
    for gap, c in requests:
        clock.advance(gap)
        last = limiter.hit("k", c)
    if last is not None:
        clock.advance(last.reset_after)
    assert limiter.hit("k", LIMIT).allowed
