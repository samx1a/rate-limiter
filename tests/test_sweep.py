"""Idle keys can be dropped from memory without changing any decision."""

import pytest

from ratelimit import FakeClock
from ratelimit.algorithms.base import MIN_SWEEP_SIZE, InMemoryLimiter
from ratelimit.algorithms.counter import NaiveCounter
from ratelimit.algorithms.fixed_window import FixedWindow
from ratelimit.algorithms.sliding_counter import SlidingWindowCounter
from ratelimit.algorithms.sliding_window import SlidingWindowLog
from ratelimit.algorithms.token_bucket import TokenBucket


def test_fixed_window_drops_keys_from_past_windows() -> None:
    clock = FakeClock()
    limiter = FixedWindow(3, 10, clock)
    limiter.hit("old")
    clock.set(12)
    limiter.hit("new")
    assert limiter.sweep() == 1
    assert len(limiter) == 1


def test_sliding_log_drops_keys_once_their_newest_entry_expires() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLog(3, 10, clock)
    limiter.hit("k")
    clock.set(5)
    limiter.hit("k")
    clock.set(14.9)
    assert limiter.sweep() == 0  # the t=5 entry still counts
    clock.set(15)
    assert limiter.sweep() == 1


def test_sliding_counter_keeps_keys_while_the_previous_window_is_in_view() -> None:
    clock = FakeClock()
    limiter = SlidingWindowCounter(3, 10, clock)
    limiter.hit("k")  # window 0
    clock.set(15)  # window 1: window 0's count is half in view
    assert limiter.sweep() == 0
    clock.set(20)  # window 2: window 0 is out of view
    assert limiter.sweep() == 1


def test_token_bucket_drops_buckets_that_have_refilled() -> None:
    clock = FakeClock()
    limiter = TokenBucket(4, 1.0, clock)
    limiter.hit("k", cost=2)
    clock.set(1.5)
    assert limiter.sweep() == 0  # 3.5 tokens, not full yet
    clock.set(2)
    assert limiter.sweep() == 1


def test_counter_never_forgets() -> None:
    limiter = NaiveCounter(3)
    limiter.hit("k")
    assert limiter.sweep() == 0


ALL = {
    "fixed_window": lambda c: FixedWindow(3, 10, c),
    "sliding_window": lambda c: SlidingWindowLog(3, 10, c),
    "sliding_counter": lambda c: SlidingWindowCounter(3, 10, c),
    "token_bucket": lambda c: TokenBucket(3, 0.3, c),
}


@pytest.mark.parametrize("name", ALL)
def test_sweeping_never_changes_a_decision(name: str) -> None:
    """Run identical traffic through two limiters, sweeping one of them constantly."""
    clock = FakeClock()
    plain: InMemoryLimiter[object] = ALL[name](clock)
    swept: InMemoryLimiter[object] = ALL[name](clock)
    dropped = 0
    for step in range(600):
        clock.set(step * 0.37)
        # k0 is busy, k1 comes back every ~11s, k2 every ~37s: some keys go idle.
        for key, every in (("k0", 1), ("k1", 30), ("k2", 100)):
            if step % every == 0:
                cost = 1 + step % 2
                assert plain.hit(key, cost) == swept.hit(key, cost), (name, step, key)
        dropped += swept.sweep()
    assert dropped > 0  # otherwise this test proves nothing


@pytest.mark.parametrize("name", ALL)
def test_memory_stays_bounded_without_calling_sweep(name: str) -> None:
    """100k one-off clients over ~3 hours. Only the last window's worth are live."""
    clock = FakeClock()
    limiter: InMemoryLimiter[object] = ALL[name](clock)
    peak = 0
    for n in range(100_000):
        clock.set(n * 0.1)  # 10 new clients per second
        limiter.hit(f"ip-{n}")
        peak = max(peak, len(limiter))
    # ~100-200 keys are live at any moment; the map may grow to 2x before a sweep.
    assert peak <= 2 * MIN_SWEEP_SIZE


def test_a_busy_map_is_not_swept_on_every_hit() -> None:
    clock = FakeClock()
    limiter = FixedWindow(3, 1e9, clock)  # nothing ever goes idle
    sweeps = 0
    original = limiter._sweep

    def counting_sweep() -> int:
        nonlocal sweeps
        sweeps += 1
        return original()

    limiter._sweep = counting_sweep  # type: ignore[method-assign]
    for n in range(20_000):
        limiter.hit(f"k{n}")
    assert sweeps <= 5  # at 1024, 2048, 4096, 8192, 16384 keys
