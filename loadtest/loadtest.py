"""HTTP load test for the rate limiter.

Two modes:

- **closed loop** (default): ``--concurrency`` workers each send a request, wait for
  the answer, and immediately send the next, for ``--duration`` seconds. Good for
  "how many requests per second can this take".
- **open loop** (``--rate N``): send N requests per second on a fixed schedule no
  matter how fast answers come back, with up to ``--concurrency`` in flight. Latency
  is measured from when each request *should* have been sent. A closed loop hides a
  slow server, because the workers slow down with it and stop sending (so-called
  coordinated omission); an open loop keeps the pressure on and the queueing delay
  shows up in the percentiles.

Requests rotate through ``--users`` user ids (``X-User-Id``). Prints throughput,
status counts, errors and latency, and checks that the server never admitted more
than the configured limit allows.

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
    mode: str = "closed"
    target_rate: float | None = None
    requests: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    transport_errors: dict[str, int] = field(default_factory=dict)
    latency_ms: dict[str, float] = field(default_factory=dict)
    # From each response's Server-Timing header: how long the limit check itself took
    # inside the server, without the network and queueing around it.
    limit_check_ms: dict[str, float] = field(default_factory=dict)
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
    if algorithm is Algorithm.COUNTER:
        return spec.requests
    # fixed window and sliding counter: at most the limit per calendar window, and a
    # run can touch one more calendar window than it spans
    return spec.requests * (windows + 1)


def server_timing_ms(header: str | None, name: str = "ratelimit") -> float | None:
    """``ratelimit;dur=0.98`` -> 0.98. None if the metric isn't there."""
    for metric in (header or "").split(","):
        parts = [part.strip() for part in metric.split(";")]
        if parts[0] != name:
            continue
        for part in parts[1:]:
            if part.startswith("dur="):
                try:
                    return float(part[4:])
                except ValueError:
                    return None
    return None


def summarize(values: list[float]) -> dict[str, float]:
    values = sorted(values)
    return {
        "p50": round(percentile(values, 50), 2),
        "p95": round(percentile(values, 95), 2),
        "p99": round(percentile(values, 99), 2),
        "max": round(values[-1], 2) if values else 0.0,
        "mean": round(statistics.fmean(values), 2) if values else 0.0,
    }


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, math.ceil(p / 100 * len(sorted_values)) - 1)
    return sorted_values[max(0, index)]


async def run(args: argparse.Namespace) -> Result:
    statuses: Counter[str] = Counter()
    transport: Counter[str] = Counter()
    latencies: list[float] = []
    checks: list[float] = []
    counter = 0
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    timeout = httpx.Timeout(args.timeout)

    async with httpx.AsyncClient(limits=limits, timeout=timeout, http2=False) as client:
        started = time.perf_counter()
        deadline = started + args.duration

        async def send(n: int, scheduled: float) -> None:
            """One request. Latency counts from ``scheduled``: in open-loop mode that
            includes time spent waiting for a free worker, as a real client would."""
            headers = {"x-user-id": f"{args.user_prefix}{n % args.users}"}
            try:
                response = await client.get(args.url, headers=headers)
                statuses[str(response.status_code)] += 1
                latencies.append((time.perf_counter() - scheduled) * 1000)
                check = server_timing_ms(response.headers.get("server-timing"))
                if check is not None:
                    checks.append(check)
            except httpx.HTTPError as exc:
                transport[type(exc).__name__] += 1

        async def closed_worker() -> None:
            nonlocal counter
            while time.perf_counter() < deadline:
                counter += 1
                await send(counter, time.perf_counter())

        # Open loop: a scheduler drops (n, scheduled time) on a queue at the target
        # rate and a fixed pool of workers sends them. Not one task per request: with a
        # backlog that's thousands of tasks contending for a semaphore, and the load
        # tool itself slows down exactly when the server does.
        queue: asyncio.Queue[tuple[int, float] | None] = asyncio.Queue()

        async def scheduler() -> None:
            for n in range(int(args.duration * args.rate)):
                scheduled = started + n / args.rate
                delay = scheduled - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                queue.put_nowait((n, scheduled))
            for _ in range(args.concurrency):
                queue.put_nowait(None)  # one stop signal per worker

        async def open_worker() -> None:
            while (item := await queue.get()) is not None:
                await send(*item)

        if args.rate:
            workers = [open_worker() for _ in range(args.concurrency)]
            await asyncio.gather(scheduler(), *workers)
        else:
            await asyncio.gather(*(closed_worker() for _ in range(args.concurrency)))
        elapsed = time.perf_counter() - started

    total = sum(statuses.values()) + sum(transport.values())
    result = Result(
        url=args.url,
        duration_s=round(elapsed, 2),
        concurrency=args.concurrency,
        users=args.users,
        mode="open" if args.rate else "closed",
        target_rate=args.rate,
        requests=total,
        statuses=dict(sorted(statuses.items())),
        transport_errors=dict(transport),
        latency_ms=summarize(latencies),
        limit_check_ms=summarize(checks) if checks else {},
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="closed loop: number of workers; open loop: most requests in flight",
    )
    parser.add_argument(
        "--rate", type=float, help="open loop: send this many requests per second on a schedule"
    )
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.rate is not None and args.rate <= 0:
        raise SystemExit("--rate must be > 0")

    result = asyncio.run(run(args))
    print(json.dumps(asdict(result), indent=2))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(asdict(result), fh, indent=2)
    return 1 if result.over_admitted else 0


if __name__ == "__main__":
    sys.exit(main())
