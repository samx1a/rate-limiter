"""Separate processes (standing in for separate app servers) share one Redis key.

Each process has its own Python interpreter, its own Redis connection and no shared
memory, so only the atomicity of the Lua script can keep the total within the limit.
"""

from __future__ import annotations

import multiprocessing as mp
import uuid
from multiprocessing.synchronize import Barrier

import pytest
import redis

from ratelimit.limits import LimitSpec
from ratelimit.redis_limiters import build_redis_limiter
from tests.conftest import REDIS_URL

pytestmark = pytest.mark.redis

PROCESSES = 8
HITS_PER_PROCESS = 150
LIMIT = 100
# A period so long that nothing refills or rolls over during the test.
SPEC = LimitSpec(LIMIT, 10**7)


def worker(algorithm: str, prefix: str, barrier: Barrier, results: mp.Queue[int | str]) -> None:
    try:
        client = redis.Redis.from_url(REDIS_URL, socket_timeout=10)
        client.ping()  # connect (and TLS handshake) before the start signal
        limiter = build_redis_limiter(algorithm, SPEC, client, prefix=prefix)
        barrier.wait(timeout=60)  # start together for maximum contention
        admitted = sum(limiter.hit("hot").allowed for _ in range(HITS_PER_PROCESS))
        client.close()
        results.put(admitted)
    except Exception as exc:  # report, don't vanish: the parent shows the error
        results.put(f"{type(exc).__name__}: {exc}")


@pytest.mark.parametrize("algorithm", ["fixed_window", "sliding_window", "token_bucket"])
def test_processes_never_exceed_the_limit_together(
    redis_client: redis.Redis, algorithm: str
) -> None:
    ctx = mp.get_context("spawn")
    prefix = f"test-dist-{uuid.uuid4().hex[:8]}"
    barrier = ctx.Barrier(PROCESSES)
    results: mp.Queue[int | str] = ctx.Queue()
    procs = [
        ctx.Process(target=worker, args=(algorithm, prefix, barrier, results))
        for _ in range(PROCESSES)
    ]
    try:
        for p in procs:
            p.start()
        replies = [results.get(timeout=120) for _ in procs]
        for p in procs:
            p.join(timeout=10)
    finally:
        keys = list(redis_client.scan_iter(f"{prefix}:*"))
        if keys:
            redis_client.delete(*keys)

    errors = [r for r in replies if isinstance(r, str)]
    assert not errors, errors
    per_process = [r for r in replies if isinstance(r, int)]
    assert sum(per_process) == LIMIT
    assert len([n for n in per_process if n > 0]) > 1  # really was shared, not one winner
