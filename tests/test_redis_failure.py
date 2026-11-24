"""What happens when Redis is down: fail open (default) or fail closed."""

import time
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient

from ratelimit import FakeClock
from ratelimit.breaker import BreakerState, CircuitBreaker
from ratelimit.decision import LimiterUnavailable
from ratelimit.limits import LimitSpec
from ratelimit.redis_limiters import RedisTokenBucket, connect
from webapp.main import create_app
from webapp.settings import Backend, Settings

DEAD_REDIS = "redis://127.0.0.1:1/0"  # nothing listens on port 1


def settings(fail_open: bool, local_fallback: bool = False) -> Settings:
    return Settings(
        backend=Backend.REDIS,
        redis_url=DEAD_REDIS,
        fail_open=fail_open,
        local_fallback=local_fallback,
        ip_limit=LimitSpec(5, 60),
        user_limit=LimitSpec(5, 60),
    )


def test_unreachable_redis_raises_limiter_unavailable() -> None:
    bucket = RedisTokenBucket(connect(DEAD_REDIS), 5, 1.0)
    with pytest.raises(LimiterUnavailable):
        bucket.hit("k")
    with pytest.raises(LimiterUnavailable):
        bucket.reset("k")


def test_fail_open_serves_requests_without_limit_headers() -> None:
    c = TestClient(create_app(settings(fail_open=True)))
    response = c.get("/api/hello", headers={"x-user-id": "alice"})
    assert response.status_code == 200
    assert "RateLimit-Limit" not in response.headers


def test_fail_closed_returns_503() -> None:
    c = TestClient(create_app(settings(fail_open=False)))
    response = c.get("/api/hello")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["detail"]["error"] == "limiter_unavailable"


def test_local_fallback_keeps_limiting_when_redis_is_down() -> None:
    c = TestClient(create_app(settings(fail_open=False, local_fallback=True)))
    codes = [c.get("/api/hello", headers={"x-user-id": "alice"}).status_code for _ in range(7)]
    assert codes == [200] * 5 + [429] * 2  # limits are 5/60s, enforced from memory
    # A new user from the same IP: the per-IP limit is held in memory too.
    response = c.get("/api/hello", headers={"x-user-id": "bob"})
    assert response.status_code == 429
    assert response.json()["detail"]["policy"] == "ip"


def test_an_outage_fails_fast() -> None:
    c = TestClient(create_app(settings(fail_open=True)))
    started = time.monotonic()
    c.get("/api/hello")
    assert time.monotonic() - started < 2


class FlakyRedis:
    """Just enough of a Redis client to count calls and fail on demand."""

    def __init__(self) -> None:
        self.calls = 0
        self.up = False

    def register_script(self, source: str) -> Any:
        def run(keys: list[str], args: list[Any]) -> list[Any]:
            self.calls += 1
            if not self.up:
                raise redis.ConnectionError("connection refused")
            return [1, 4, "0", "1000"]

        return run


def test_breaker_stops_calling_redis_during_an_outage() -> None:
    client = FlakyRedis()
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=3, cooldown=5, clock=clock)
    bucket = RedisTokenBucket(client, 5, 1.0, breaker=breaker)  # type: ignore[arg-type]
    for _ in range(10):
        with pytest.raises(LimiterUnavailable):
            bucket.hit("k")
    assert client.calls == 3  # the other 7 failed instantly without touching Redis
    assert breaker.state is BreakerState.OPEN

    client.up = True
    clock.advance(5)
    assert bucket.hit("k").allowed  # the trial call goes through and closes it
    assert breaker.state is BreakerState.CLOSED


def test_a_script_error_still_finishes_the_half_open_trial() -> None:
    class BrokenScript(FlakyRedis):
        def register_script(self, source: str) -> Any:
            def run(keys: list[str], args: list[Any]) -> list[Any]:
                raise redis.ResponseError("ERR user_script:1: boom")

            return run

    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown=5, clock=clock)
    breaker.record_failure()
    clock.advance(5)
    bucket = RedisTokenBucket(BrokenScript(), 5, 1.0, breaker=breaker)  # type: ignore[arg-type]
    with pytest.raises(redis.ResponseError):
        bucket.hit("k")  # a real bug propagates...
    assert breaker.state is BreakerState.CLOSED  # ...but Redis answered, so it's up
