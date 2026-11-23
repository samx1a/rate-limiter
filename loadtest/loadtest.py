"""Closed-loop HTTP load test for the rate limiter.

``--concurrency`` workers each send a request, wait for the answer, and immediately
send the next, for ``--duration`` seconds. Requests rotate through ``--users`` user ids
(``X-User-Id``). Prints throughput, status counts, errors and latency, and checks that
the server never admitted more than the configured limit allows.

Example:
    python -m loadtest.loadtest --url http://127.0.0.1:8000/api/hello \\
        --duration 20 --concurrency 50 --users 1 --limit 10/10s --algorithm token_bucket

"Errors" are transport failures (timeouts, refused connections) plus any status
other than 200 or 429. A 429 is the limiter working, not an error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

import httpx2 as httpx

from ratelimit.limits import Algorithm, LimitSpec, parse_limit


@dataclass
class Result:
    url: str
    duration_s: float
    concurrency: int
    users: int
    requests: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    transport_errors: dict[str, int] = field(default_factory=dict)
    latency_ms: dict[str, float] = field(default_factory=dict)
    requests_per_s: float = 0.0
    errors: int = 0
    admitted: int = 0
    admitted_max: int | None = None
    over_admitted: bool | None = None
    started_at: str = ""
    machine: str = ""


def max_admissible(algorithm: Algorithm, spec: LimitSpec, seconds: float) -> int:
    """The most one key may be admitted in ``seconds`` (an upper bound)."""
    if algorithm is Algorithm.TOKEN_BUCKET:
        return math.floor(spec.requests + spec.rate * seconds)
    windows = math.ceil(seconds / spec.period)
    if algorithm is Algorithm.SLIDING_WINDOW:
        return spec.requests * windows
    # fixed window: a run can touch one more calendar window than it spans
    return spec.requests * (windows + 1)


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, math.ceil(p / 100 * len(sorted_values)) - 1)
    return sorted_values[max(0, index)]


async def run(args: argparse.Namespace) -> Result:
    statuses: Counter[str] = Counter()
    transport: Counter[str] = Counter()
    latencies: list[float] = []
    counter = 0
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    timeout = httpx.Timeout(args.timeout)

    async with httpx.AsyncClient(limits=limits, timeout=timeout, http2=False) as client:
        started = time.perf_counter()
        deadline = started + args.duration

        async def worker() -> None:
            nonlocal counter
            while time.perf_counter() < deadline:
                counter += 1
                headers = {"x-user-id": f"{args.user_prefix}{counter % args.users}"}
                sent = time.perf_counter()
                try:
                    response = await client.get(args.url, headers=headers)
                    statuses[str(response.status_code)] += 1
                    latencies.append((time.perf_counter() - sent) * 1000)
                except httpx.HTTPError as exc:
                    transport[type(exc).__name__] += 1

        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        elapsed = time.perf_counter() - started

    latencies.sort()
    total = sum(statuses.values()) + sum(transport.values())
    result = Result(
        url=args.url,
        duration_s=round(elapsed, 2),
        concurrency=args.concurrency,
        users=args.users,
        requests=total,
        statuses=dict(sorted(statuses.items())),
        transport_errors=dict(transport),
        latency_ms={
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "p99": round(percentile(latencies, 99), 2),
            "max": round(latencies[-1], 2) if latencies else 0.0,
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0.0,
        },
        requests_per_s=round(total / elapsed, 1),
        errors=sum(transport.values())
        + sum(n for code, n in statuses.items() if code not in {"200", "429"}),
        admitted=statuses.get("200", 0),
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        machine=f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
    )
    if args.limit:
        per_key = max_admissible(Algorithm(args.algorithm), parse_limit(args.limit), elapsed)
        keys = args.limit_keys if args.limit_keys is not None else args.users
        result.admitted_max = per_key * keys
        result.over_admitted = result.admitted > result.admitted_max
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--user-prefix", default="lt-")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--limit", help="the binding limit on the server, e.g. 10/10s")
    parser.add_argument(
        "--limit-keys",
        type=int,
        help="how many independent keys that limit applies to (default: --users). "
        "Use 1 when one per-IP limit covers all traffic from this machine.",
    )
    parser.add_argument("--algorithm", default="token_bucket", choices=[a.value for a in Algorithm])
    parser.add_argument("--json", help="also write the result to this file")
    args = parser.parse_args(argv)

    result = asyncio.run(run(args))
    print(json.dumps(asdict(result), indent=2))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(asdict(result), fh, indent=2)
    return 1 if result.over_admitted else 0


if __name__ == "__main__":
    sys.exit(main())
