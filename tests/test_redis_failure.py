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


def test_ready_reports_503_while_health_stays_ok() -> None:
    c = TestClient(create_app(settings(fail_open=True)))
    ready = c.get("/api/ready")
    assert ready.status_code == 503
    assert ready.json() == {
        "ok": False,
        "backend": "redis",
        "error": "ConnectionError",
        "breaker": "closed",
    }
    assert c.get("/api/health").status_code == 200  # don't restart over a Redis outage


def test_breaker_state_shows_up_in_ready_and_metrics() -> None:
    c = TestClient(create_app(settings(fail_open=True, local_fallback=True)))
    for _ in range(3):  # default threshold: 3 failures in a row
        c.get("/api/hello")
    assert c.get("/api/ready").json()["breaker"] == "open"
    assert "ratelimit_breaker_open 1" in c.get("/api/metrics").text


def test_503_retry_after_matches_the_breaker_cooldown() -> None:
    dead = Settings(
        backend=Backend.REDIS,
        redis_url=DEAD_REDIS,
        fail_open=False,
        local_fallback=False,
        breaker_threshold=1,
        breaker_cooldown=7.0,
    )
    c = TestClient(create_app(dead))
    c.get("/api/hello")  # trips the breaker
    response = c.get("/api/hello")
    assert response.status_code == 503
    assert response.headers["Retry-After"] in {"6", "7"}  # not "1"


def test_memory_keys_count_the_fallback_during_an_outage() -> None:
    c = TestClient(create_app(settings(fail_open=False, local_fallback=True)))
    assert "ratelimit_memory_keys 0" in c.get("/api/metrics").text
    c.get("/api/hello", headers={"x-user-id": "alice"})
    assert "ratelimit_memory_keys 2" in c.get("/api/metrics").text  # IP + alice


def test_an_unreachable_host_fails_within_one_timeout() -> None:
    """10.255.255.1 is unroutable, so the connect times out rather than being refused.
    That's the slow kind of outage; it must cost one timeout, not one per retry."""
    client = connect("redis://10.255.255.1:6379/0", timeout=0.25, connect_timeout=0.25)
    bucket = RedisTokenBucket(client, 5, 1.0)
    started = time.monotonic()
    with pytest.raises(LimiterUnavailable):
        bucket.hit("k")
    assert time.monotonic() - started < 0.6
    assert client.connection_pool.connection_kwargs["retry"].get_retries() == 0
