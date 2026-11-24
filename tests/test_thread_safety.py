"""Many threads hitting one key must never get more than the limit through.

Python switches threads every 5ms by default, which hides races. These tests drop the
switch interval to ~1 microsecond so threads interleave as often as possible, and
release all threads at once with a barrier.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from ratelimit import FakeClock, RateLimiter
from ratelimit.algorithms.counter import NaiveCounter
from ratelimit.algorithms.fixed_window import FixedWindow
from ratelimit.algorithms.sliding_counter import SlidingWindowCounter
from ratelimit.algorithms.sliding_window import SlidingWindowLog
from ratelimit.algorithms.token_bucket import TokenBucket

THREADS = 32
HITS_PER_THREAD = 200
LIMIT = 100

# Time is frozen, so nothing refills or slides: exactly LIMIT may get through.
FACTORIES: dict[str, Callable[[], RateLimiter]] = {
    "counter": lambda: NaiveCounter(LIMIT),
    "fixed_window": lambda: FixedWindow(LIMIT, 60, FakeClock()),
    "sliding_window": lambda: SlidingWindowLog(LIMIT, 60, FakeClock()),
    "sliding_counter": lambda: SlidingWindowCounter(LIMIT, 60, FakeClock()),
    "token_bucket": lambda: TokenBucket(LIMIT, 1.0, FakeClock()),
}


@pytest.fixture(autouse=True)
def frequent_thread_switches() -> Iterator[None]:
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


def hammer(limiter: RateLimiter, key_for: Callable[[int], str]) -> list[int]:
    """Run THREADS threads x HITS_PER_THREAD hits; return admitted count per thread."""
    barrier = threading.Barrier(THREADS)
    admitted = [0] * THREADS

    def worker(n: int) -> None:
        barrier.wait()
        for _ in range(HITS_PER_THREAD):
            if limiter.hit(key_for(n)).allowed:
                admitted[n] += 1

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return admitted


@pytest.mark.parametrize("name", FACTORIES)
def test_one_hot_key_never_exceeds_the_limit(name: str) -> None:
    admitted = hammer(FACTORIES[name](), key_for=lambda n: "hot")
    assert sum(admitted) == LIMIT


@pytest.mark.parametrize("name", FACTORIES)
def test_separate_keys_do_not_interfere(name: str) -> None:
    admitted = hammer(FACTORIES[name](), key_for=lambda n: f"user-{n}")
    assert admitted == [min(LIMIT, HITS_PER_THREAD)] * THREADS


@pytest.mark.parametrize("name", ["sliding_window", "token_bucket"])
def test_remaining_is_consistent_under_contention(name: str) -> None:
    """Every admitted request sees a distinct 'remaining' value: no two threads
    were handed the same slot."""
    limiter = FACTORIES[name]()
    seen: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(THREADS)

    def worker() -> None:
        barrier.wait()
        for _ in range(HITS_PER_THREAD):
            d = limiter.hit("hot")
            if d.allowed:
                with lock:
                    seen.append(d.remaining)

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == list(range(LIMIT))


def test_token_bucket_with_a_real_clock_stays_within_its_rate() -> None:
    capacity, rate = 20, 50.0
    bucket = TokenBucket(capacity, rate)
    stop = time.monotonic() + 0.5
    admitted = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal admitted
        while time.monotonic() < stop:
            if bucket.hit("hot").allowed:
                with lock:
                    admitted += 1

    started = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    assert admitted <= capacity + rate * elapsed + 1
