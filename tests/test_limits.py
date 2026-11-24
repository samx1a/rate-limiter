import pytest

from ratelimit import FakeClock
from ratelimit.algorithms.counter import NaiveCounter
from ratelimit.algorithms.fixed_window import FixedWindow
from ratelimit.algorithms.sliding_counter import SlidingWindowCounter
from ratelimit.algorithms.sliding_window import SlidingWindowLog
from ratelimit.algorithms.token_bucket import TokenBucket
from ratelimit.limits import Algorithm, LimitSpec, build_limiter, parse_limit


@pytest.mark.parametrize(
    ("text", "requests", "period"),
    [
        ("10/s", 10, 1),
        ("10/10s", 10, 10),
        ("100/1m", 100, 60),
        ("100 / 2 min", 100, 120),
        ("5/1.5h", 5, 5400),
        ("1000/1d", 1000, 86400),
        ("3/SEC", 3, 1),
    ],
)
def test_parse_limit(text: str, requests: int, period: float) -> None:
    assert parse_limit(text) == LimitSpec(requests, period)


@pytest.mark.parametrize("text", ["", "10", "10/", "10/10", "0/s", "10/0s", "-1/s", "10/5y", "a/s"])
def test_parse_limit_rejects_bad_input(text: str) -> None:
    with pytest.raises(ValueError):
        parse_limit(text)


def test_spec_rate_and_str() -> None:
    spec = parse_limit("30/1m")
    assert spec.rate == 0.5
    assert str(spec) == "30/60s"


@pytest.mark.parametrize(
    ("algorithm", "cls"),
    [
        ("counter", NaiveCounter),
        ("fixed_window", FixedWindow),
        ("sliding_window", SlidingWindowLog),
        ("sliding_counter", SlidingWindowCounter),
        (Algorithm.TOKEN_BUCKET, TokenBucket),
    ],
)
def test_build_limiter(algorithm: str, cls: type) -> None:
    limiter = build_limiter(algorithm, LimitSpec(5, 10), FakeClock())
    assert isinstance(limiter, cls)
    assert limiter.limit == 5


def test_token_bucket_refills_at_requests_per_period() -> None:
    clock = FakeClock()
    bucket = build_limiter("token_bucket", parse_limit("10/10s"), clock)
    for _ in range(10):
        bucket.hit("k")
    clock.advance(1)  # 1 token per second
    assert bucket.hit("k").allowed
    assert not bucket.hit("k").allowed


def test_unknown_algorithm() -> None:
    with pytest.raises(ValueError):
        build_limiter("leaky", LimitSpec(1, 1))
