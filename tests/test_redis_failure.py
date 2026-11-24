"""What happens when Redis is down: fail open (default) or fail closed."""

import time

import pytest
from fastapi.testclient import TestClient

from ratelimit.decision import LimiterUnavailable
from ratelimit.limits import LimitSpec
from ratelimit.redis_limiters import RedisTokenBucket, connect
from webapp.main import create_app
from webapp.settings import Backend, Settings

DEAD_REDIS = "redis://127.0.0.1:1/0"  # nothing listens on port 1


def settings(fail_open: bool) -> Settings:
    return Settings(
        backend=Backend.REDIS,
        redis_url=DEAD_REDIS,
        fail_open=fail_open,
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


def test_an_outage_fails_fast() -> None:
    c = TestClient(create_app(settings(fail_open=True)))
    started = time.monotonic()
    c.get("/api/hello")
    assert time.monotonic() - started < 2
