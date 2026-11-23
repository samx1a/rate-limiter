import pytest

from ratelimit import Decision, FakeClock


def test_fake_clock_moves_only_forward() -> None:
    clock = FakeClock(start=10.0)
    clock.advance(2.5)
    assert clock.now() == 12.5
    with pytest.raises(ValueError):
        clock.advance(-1)
    with pytest.raises(ValueError):
        clock.set(5.0)


def test_decision_rejects_impossible_states() -> None:
    with pytest.raises(ValueError):
        Decision(allowed=True, limit=5, remaining=-1, reset_after=1.0)
    with pytest.raises(ValueError):
        Decision(allowed=True, limit=5, remaining=1, reset_after=1.0, retry_after=2.0)
