"""How a client should behave when it gets rate limited.

    python -m examples.client https://rate-limiter-lac.vercel.app/api/hello --user me

The server says exactly when to come back (``Retry-After``), so a good client:

1. waits that long instead of hammering, and
2. adds a little random jitter on top. If 1,000 clients all get "retry after 3
   seconds" at once, without jitter they all come back in the same millisecond and
   get blocked together again (a thundering herd).

For network errors there is no Retry-After, so it backs off exponentially with "full
jitter" (a random wait between 0 and the backoff), which spreads retries out best.
"""

from __future__ import annotations

import argparse
import random
import time
from collections.abc import Callable

import httpx2 as httpx

RETRYABLE = {429, 503}


def retry_delay(
    response: httpx.Response | None,
    attempt: int,
    *,
    base: float = 0.5,
    cap: float = 30.0,
    jitter: float = 0.1,
    rand: Callable[[], float] = random.random,
) -> float:
    """Seconds to wait before attempt number ``attempt + 1``.

    With a Retry-After header: that many seconds plus up to ``jitter`` (10%) more,
    never less, since retrying early just gets another 429. Without one: full jitter
    on an exponential backoff, ``random(0, min(cap, base * 2**attempt))``.
    """
    wait = _seconds(response.headers.get("retry-after")) if response is not None else None
    if wait is not None:
        return min(cap, wait * (1 + jitter * rand()))
    return rand() * min(cap, base * 2.0**attempt)


def _seconds(header: str | None) -> float | None:
    """Retry-After as seconds, or None if missing or an HTTP-date (this server never
    sends dates, so a date falls back to backoff rather than parsing it)."""
    if header is None:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        return None


def get_with_retries(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    attempts: int = 5,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> httpx.Response:
    """GET ``url``, waiting and retrying on 429/503 and network errors. Returns the
    last response, or raises the last network error if every attempt failed."""
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            response = client.get(url, headers=headers)
        except httpx.TransportError:
            if last:
                raise
            sleep(retry_delay(None, attempt, rand=rand))
            continue
        if response.status_code not in RETRYABLE or last:
            return response
        sleep(retry_delay(response, attempt, rand=rand))
    raise AssertionError("unreachable")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url")
    parser.add_argument("--user", default="example-client")
    parser.add_argument("--requests", type=int, default=15)
    args = parser.parse_args()
    with httpx.Client(timeout=10) as client:
        for n in range(1, args.requests + 1):
            started = time.monotonic()
            response = get_with_retries(client, args.url, headers={"x-user-id": args.user})
            waited = time.monotonic() - started
            remaining = response.headers.get("ratelimit-remaining", "-")
            print(f"{n:>3}  {response.status_code}  remaining={remaining}  took {waited:.1f}s")


if __name__ == "__main__":
    main()
