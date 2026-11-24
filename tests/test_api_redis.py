"""The full HTTP path with Redis as the store."""

import pytest
import redis
from fastapi.testclient import TestClient

from ratelimit.limits import LimitSpec
from tests.conftest import REDIS_URL
from webapp.main import create_app
from webapp.settings import Backend, Settings

pytestmark = pytest.mark.redis


def test_two_app_instances_share_limits_through_redis(
    redis_client: redis.Redis, prefix: str
) -> None:
    settings = Settings(
        backend=Backend.REDIS,
        redis_url=REDIS_URL,
        key_prefix=prefix,
        ip_limit=LimitSpec(100, 60),
        user_limit=LimitSpec(3, 60),
        trusted_ip_header="x-real-ip",
        redis_timeout=5.0,  # the test may run against a far-away Redis
    )
    server_a, server_b = TestClient(create_app(settings)), TestClient(create_app(settings))
    headers = {"x-real-ip": "203.0.113.1", "x-user-id": "alice"}
    codes = [
        server_a.get("/api/hello", headers=headers).status_code,
        server_b.get("/api/hello", headers=headers).status_code,
        server_a.get("/api/hello", headers=headers).status_code,
        server_b.get("/api/hello", headers=headers).status_code,
    ]
    assert codes == [200, 200, 200, 429]  # 200s here are real decisions, not fail-open
    assert server_a.get("/api/info").json()["backend"] == "redis"


def test_shutdown_closes_the_redis_client(
    redis_client: redis.Redis, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import webapp.main

    created: list[redis.Redis] = []
    real_connect = webapp.main.connect

    def tracking_connect(*args: object, **kwargs: object) -> redis.Redis:
        client = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        created.append(client)
        return client

    monkeypatch.setattr(webapp.main, "connect", tracking_connect)
    settings = Settings(
        backend=Backend.REDIS, redis_url=REDIS_URL, key_prefix=prefix, redis_timeout=5.0
    )
    with TestClient(create_app(settings)) as c:
        c.get("/api/hello")
        pool = created[0].connection_pool
        connections = list(pool._available_connections)  # type: ignore[attr-defined]
        assert connections and all(conn._sock is not None for conn in connections)
    assert all(conn._sock is None for conn in connections)  # sockets closed on shutdown
