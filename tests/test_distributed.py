"""Separate processes (standing in for separate app servers) share one Redis key.

Each process has its own Python interpreter, its own Redis connection and no shared
memory, so only the atomicity of the Lua script can keep the total within the limit.
"""

from __future__ import annotations

import multiprocessing as mp
import uuid
from collections.abc import Iterator
from multiprocessing.synchronize import Barrier

import pytest
import redis

from ratelimit.limits import LimitSpec
from ratelimit.redis_limiters import build_redis_limiter, hit_many
from tests.conftest import REDIS_URL

pytestmark = pytest.mark.redis

PROCESSES = 8
HITS_PER_PROCESS = 150
LIMIT = 100
# A period so long that nothing refills or rolls over during the test.
SPEC = LimitSpec(LIMIT, 10**7)
# For the batched test: a looser "IP" limit checked together with SPEC in one script.
OUTER = LimitSpec(LIMIT + 50, 10**7)
ALGORITHMS = ["fixed_window", "sliding_window", "sliding_counter", "token_bucket"]


def worker(
    batched: bool, algorithm: str, prefix: str, barrier: Barrier, results: mp.Queue[int | str]
) -> None:
    try:
        client = redis.Redis.from_url(REDIS_URL, socket_timeout=10)
        client.ping()  # connect (and TLS handshake) before the start signal
        limiter = build_redis_limiter(algorithm, SPEC, client, prefix=prefix)
        outer = build_redis_limiter(algorithm, OUTER, client, prefix=f"{prefix}:outer")
        barrier.wait(timeout=60)  # start together for maximum contention
        admitted = 0
        for _ in range(HITS_PER_PROCESS):
            if batched:
                decisions = hit_many([(outer, "hot", 1), (limiter, "hot", 1)])
                admitted += len(decisions) == 2 and all(d.allowed for d in decisions)
            else:
                admitted += limiter.hit("hot").allowed
        client.close()
        results.put(admitted)
    except Exception as exc:  # report, don't vanish: the parent shows the error
        results.put(f"{type(exc).__name__}: {exc}")


def run_workers(batched: bool, algorithm: str, prefix: str) -> list[int]:
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(PROCESSES)
    results: mp.Queue[int | str] = ctx.Queue()
    procs = [
        ctx.Process(target=worker, args=(batched, algorithm, prefix, barrier, results))
        for _ in range(PROCESSES)
    ]
    for p in procs:
        p.start()
    replies = [results.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=10)
    errors = [r for r in replies if isinstance(r, str)]
    assert not errors, errors
    per_process = [r for r in replies if isinstance(r, int)]
    assert len([n for n in per_process if n > 0]) > 1  # really was shared, not one winner
    return per_process


@pytest.fixture
def dist_prefix(redis_client: redis.Redis) -> Iterator[str]:
    prefix = f"test-dist-{uuid.uuid4().hex[:8]}"
    yield prefix
    keys = list(redis_client.scan_iter(f"{prefix}:*"))
    if keys:
        redis_client.delete(*keys)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_processes_never_exceed_the_limit_together(algorithm: str, dist_prefix: str) -> None:
    assert sum(run_workers(False, algorithm, dist_prefix)) == LIMIT


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_batched_checks_are_all_or_nothing_across_processes(
    redis_client: redis.Redis, algorithm: str, dist_prefix: str
) -> None:
    """8 processes check an outer limit (150) and an inner one (100) together. Exactly
    100 get through, and the outer limit was charged for exactly those 100: the
    other 1,100 were blocked by the inner limit and must not have spent anything."""
    assert sum(run_workers(True, algorithm, dist_prefix)) == LIMIT
    outer = build_redis_limiter(algorithm, OUTER, redis_client, prefix=f"{dist_prefix}:outer")
    last_50 = outer.hit("hot", cost=50)
    assert last_50.allowed and last_50.remaining == 0  # 150 - 100 spent - 50 now
