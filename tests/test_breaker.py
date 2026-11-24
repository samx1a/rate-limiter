import threading

import pytest

from ratelimit import FakeClock
from ratelimit.breaker import BreakerState, CircuitBreaker


def make(threshold: int = 3, cooldown: float = 5.0) -> tuple[CircuitBreaker, FakeClock]:
    clock = FakeClock()
    return CircuitBreaker(threshold, cooldown, clock), clock


def test_stays_closed_below_the_threshold() -> None:
    breaker, _ = make(threshold=3)
    for _ in range(2):
        assert breaker.allow()
        breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED and breaker.allow()


def test_a_success_resets_the_failure_count() -> None:
    breaker, _ = make(threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED


def test_opens_after_threshold_failures_in_a_row() -> None:
    breaker, clock = make(threshold=3, cooldown=5)
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allow()
    clock.advance(4.9)
    assert not breaker.allow()


def test_half_open_lets_exactly_one_trial_through() -> None:
    breaker, clock = make(threshold=1, cooldown=5)
    breaker.record_failure()
    clock.advance(5)
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow()
    assert not breaker.allow()  # the trial is still running


def test_successful_trial_closes_it() -> None:
    breaker, clock = make(threshold=1, cooldown=5)
    breaker.record_failure()
    clock.advance(5)
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.allow() and breaker.allow()


def test_failed_trial_opens_it_for_another_cooldown() -> None:
    breaker, clock = make(threshold=3, cooldown=5)
    for _ in range(3):
        breaker.record_failure()
    clock.advance(5)
    assert breaker.allow()
    breaker.record_failure()  # one failure is enough while half-open
    assert breaker.state is BreakerState.OPEN
    clock.advance(4.9)
    assert not breaker.allow()
    clock.advance(0.1)
    assert breaker.allow()


def test_only_one_thread_gets_the_trial() -> None:
    breaker, clock = make(threshold=1, cooldown=5)
    breaker.record_failure()
    clock.advance(5)
    barrier = threading.Barrier(16)
    allowed: list[bool] = []

    def worker() -> None:
        barrier.wait()
        allowed.append(breaker.allow())

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert allowed.count(True) == 1


@pytest.mark.parametrize(("threshold", "cooldown"), [(0, 1.0), (1, 0.0)])
def test_rejects_bad_config(threshold: int, cooldown: float) -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(threshold, cooldown)
