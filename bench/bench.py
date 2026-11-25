"""Micro-benchmarks: what one limit check costs, per algorithm and backend.

    python -m bench.bench                     # in-memory only
    python -m bench.bench --redis redis://127.0.0.1:6399/0

In memory, each algorithm is timed on one hot key and on 100k distinct keys (bigger
dicts, more cache misses, and the auto-sweep kicking in). With --redis it also times
one key per round trip against two keys checked one by one vs. batched in one script.
Reports the median of several repeats, in microseconds per check.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
import uuid
from collections.abc import Callable

from ratelimit.decision import RateLimiter
from ratelimit.limits import Algorithm, LimitSpec, build_limiter

# Big enough that nothing gets blocked: we're timing the check, not the 429 path.
SPEC = LimitSpec(10**9, 1)
TIMED = [a for a in Algorithm if a is not Algorithm.COUNTER] + [Algorithm.COUNTER]


def per_call_us(fn: Callable[[int], object], calls: int, repeats: int) -> float:
    """Median over ``repeats`` runs of ``calls`` calls, in microseconds per call."""
    runs = []
    for _ in range(repeats):
        started = time.perf_counter()
        for i in range(calls):
            fn(i)
        runs.append((time.perf_counter() - started) / calls * 1e6)
    return round(statistics.median(runs), 3)


def bench_memory(calls: int, repeats: int) -> dict[str, dict[str, float]]:
    keys = [f"user-{n}" for n in range(100_000)]
    results = {}
    for algorithm in TIMED:
        hot = build_limiter(algorithm, SPEC)
        spread = build_limiter(algorithm, SPEC)

        def one_key(_: int, limiter: RateLimiter = hot) -> object:
            return limiter.hit("k")

        def many_keys(i: int, limiter: RateLimiter = spread) -> object:
            return limiter.hit(keys[i % len(keys)])

        results[str(algorithm)] = {
            "one_hot_key_us": per_call_us(one_key, calls, repeats),
            "100k_keys_us": per_call_us(many_keys, calls, repeats),
        }
    return results


def bench_redis(url: str, calls: int, repeats: int) -> dict[str, float]:
    from ratelimit.redis_limiters import build_redis_limiter, connect, hit_many

    client = connect(url, timeout=5.0)
    prefix = f"bench-{uuid.uuid4().hex[:8]}"
    ip = build_redis_limiter(Algorithm.TOKEN_BUCKET, SPEC, client, prefix=f"{prefix}:ip")
    user = build_redis_limiter(Algorithm.TOKEN_BUCKET, SPEC, client, prefix=f"{prefix}:user")
    try:
        return {
            "ping_us": per_call_us(lambda _: client.ping(), calls, repeats),
            "one_key_us": per_call_us(lambda _: user.hit("k"), calls, repeats),
            "two_keys_one_by_one_us": per_call_us(
                lambda _: (ip.hit("1.2.3.4"), user.hit("k")), calls, repeats
            ),
            "two_keys_batched_us": per_call_us(
                lambda _: hit_many([(ip, "1.2.3.4", 1), (user, "k", 1)]), calls, repeats
            ),
        }
    finally:
        keys = list(client.scan_iter(f"{prefix}:*"))
        if keys:
            client.delete(*keys)
        client.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--calls", type=int, default=200_000, help="in-memory calls per run")
    parser.add_argument("--redis-calls", type=int, default=5_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--redis", help="also benchmark Redis at this URL")
    args = parser.parse_args(argv)
    report: dict[str, object] = {
        "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
        "memory": bench_memory(args.calls, args.repeats),
    }
    if args.redis:
        report["redis"] = bench_redis(args.redis, args.redis_calls, args.repeats)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
