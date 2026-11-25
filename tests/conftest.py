"""Shared fixtures.

Redis tests use ``REDIS_URL`` (default: redis://127.0.0.1:6379/15). If Redis is not
reachable they are skipped locally, but with ``REQUIRE_REDIS=1`` (set in CI) they
fail instead, so a misconfigured CI can never pass by silently skipping them.

Tests only touch their own uniquely prefixed keys and clean them up, so pointing them
at a Redis you use for other things is safe. The one exception is anything that
affects the whole server (like flushing the script cache): that needs
``REDIS_DISPOSABLE=1``, which says this Redis exists only for the tests (CI sets it).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/15")


@pytest.fixture(scope="session")
def redis_client() -> Iterator[redis.Redis]:
    client = redis.Redis.from_url(REDIS_URL, socket_timeout=2)
    try:
        client.ping()
    except redis.RedisError as exc:
        if os.environ.get("REQUIRE_REDIS") == "1":
            pytest.fail(f"Redis is required but unreachable at {REDIS_URL}: {exc}")
        pytest.skip(f"Redis not reachable at {REDIS_URL}")
    yield client
    client.close()


@pytest.fixture
def prefix(redis_client: redis.Redis) -> Iterator[str]:
    """A unique key prefix per test, cleaned up afterwards (never FLUSHDB a shared db)."""
    value = f"test-{uuid.uuid4().hex[:12]}"
    yield value
    keys = list(redis_client.scan_iter(f"{value}:*"))
    if keys:
        redis_client.delete(*keys)


@pytest.fixture
def disposable_redis(redis_client: redis.Redis) -> redis.Redis:
    """The Redis client, but only if it's OK to do server-wide things to it."""
    if os.environ.get("REDIS_DISPOSABLE") != "1":
        pytest.skip("touches the whole Redis server; set REDIS_DISPOSABLE=1 to allow")
    return redis_client
