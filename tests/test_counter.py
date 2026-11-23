import math

import pytest

from ratelimit.algorithms.counter import NaiveCounter


def test_allows_up_to_limit_then_blocks() -> None:
    counter = NaiveCounter(limit=3)
    results = [counter.hit("alice") for _ in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0, 0]


def test_keys_are_independent() -> None:
    counter = NaiveCounter(limit=1)
    assert counter.hit("alice").allowed
    assert not counter.hit("alice").allowed
    assert counter.hit("bob").allowed


def test_never_resets_on_its_own() -> None:
    counter = NaiveCounter(limit=1)
    counter.hit("alice")
    blocked = counter.hit("alice")
    assert blocked.reset_after == math.inf
    assert blocked.retry_after == math.inf


def test_cost_counts_multiple_units_and_blocked_requests_are_free() -> None:
    counter = NaiveCounter(limit=5)
    assert counter.hit("k", cost=4).remaining == 1
    blocked = counter.hit("k", cost=2)  # would go over: blocked, not recorded
    assert not blocked.allowed and blocked.remaining == 1
    assert counter.hit("k", cost=1).allowed


def test_cost_above_limit_is_always_blocked() -> None:
    assert not NaiveCounter(limit=2).hit("k", cost=3).allowed


def test_reset_clears_a_key() -> None:
    counter = NaiveCounter(limit=1)
    counter.hit("k")
    counter.reset("k")
    assert counter.hit("k").allowed


@pytest.mark.parametrize("cost", [0, -1, 1.5, True])
def test_rejects_bad_cost(cost: object) -> None:
    with pytest.raises(ValueError):
        NaiveCounter(limit=1).hit("k", cost=cost)  # type: ignore[arg-type]


def test_rejects_bad_limit() -> None:
    with pytest.raises(ValueError):
        NaiveCounter(limit=0)
