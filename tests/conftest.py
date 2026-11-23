"""Shared fixtures.

Redis tests use ``REDIS_URL`` (default: redis://127.0.0.1:6379/15). If Redis is not
reachable they are skipped locally, but with ``REQUIRE_REDIS=1`` (set in CI) they
fail instead, so a misconfigured CI can never pass by silently skipping them.
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
