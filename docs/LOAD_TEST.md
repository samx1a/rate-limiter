# Load tests

These numbers come from real runs of [`loadtest/loadtest.py`](../loadtest/loadtest.py).
The raw output for each run is saved in [`docs/loadtest/`](loadtest/).

How the tool works: it keeps N clients busy, and each one sends a request, waits for
the answer and sends the next one, for a fixed amount of time. It counts status codes,
measures latency, and checks that the server never let through more than the limit
allows. If it did, the tool exits with an error.

What I count as an error: a request that failed at the network level (timeout, refused
connection) or got a status other than 200 or 429. A 429 means the limiter is doing its
job, so it's not an error.

## Local (2026-11-23)

Machine: Apple M5 laptop, 10 cores, Python 3.11. The load tool, the server and Redis
all ran on the same laptop, so they were competing for CPU. The load tool is a single
Python process and it maxed out first, so treat these as a lower bound.

Server settings: token bucket, `USER_LIMIT=10/10s`, and `IP_LIMIT=1000000/1s` so the
per-IP limit doesn't get in the way (every request came from 127.0.0.1).

| Run | Backend | Server processes | Clients | Users | Requests | Req/s | 200 | 429 | Errors | Latency p50 / p95 / p99 | Let through / max |
|-----|---------|------------------|---------|-------|----------|-------|-----|-----|--------|-------------------------|-------------------|
| A | in memory | 1 | 50 | 100 | 66,317 | 3,305 | 2,900 | 63,417 | 0 | 14 / 19 / 36 ms | 2,900 / 3,000 |
| B | Redis | 4 | 100 | 1 | 49,407 | 2,460 | 29 | 49,378 | 0 | 38 / 53 / 82 ms | 29 / 30 |
| C | Redis | 4 | 100 | 1,000 | 50,020 | 2,490 | 29,000 | 21,020 | 0 | 39 / 48 / 56 ms | 29,000 / 30,000 |

Each run lasted 20 seconds. "Max" is what the limit allows in that time: for one user
that's 10 to start plus 1 per second, so 30.

Run B is the one I cared about most. Four separate server processes, each with its own
memory and its own Redis connection, got about 49,000 requests for the same user and let
29 through. If the check and the update weren't one atomic step, the four processes
would have let in more than 30.

The jump in latency from A to B is mostly queueing with 100 clients on one laptop, plus
one Redis round trip per check. After run C, all 1,000 user keys expired from Redis by
themselves about 11 seconds later, which is the refill time plus a 1 second margin.

## Closed loop vs open loop (2026-11-25)

The runs above are closed loop: 100 clients that each wait for an answer before
sending again. When the server slows down, the clients slow down with it and send
less, so the latency numbers stay flattering. Real users don't wait for each other.
So I added an open-loop mode (`--rate`) that sends on a fixed schedule and measures
each request from when it should have gone out.

Same setup as run C (Redis, 4 server processes, 1,000 users, token bucket 10/10s),
15 seconds each, laptop busy with other stuff so the absolute numbers are lower than
on 11-23:

| Run | Mode | Target | Got | 200 | 429 | Errors | Latency p50 / p95 / p99 | Limit check p50 / p99 | Let through / max |
|-----|------|--------|-----|-----|-----|--------|-------------------------|-----------------------|-------------------|
| E | closed, 100 clients | - | 2,304 req/s | 24,000 | 10,764 | 0 | 40 / 67 / 88 ms | 0.18 / 0.96 ms | 24,000 / 25,000 |
| F1 | open | 1,500 req/s | 1,482 req/s | 22,500 | 0 | 0 | 89 / 141 / 210 ms | 0.21 / 0.64 ms | 22,500 / 25,000 |
| F2 | open | 2,500 req/s | 1,644 req/s | 31,500 | 6,000 | 0 | 3,786 / 7,536 / 7,709 ms | 0.34 / 1.85 ms | 31,500 / 32,000 |

I ran all three again right after and got the same picture (closed 1,807 req/s with
p99 79 ms; open 1,500 kept up with p99 166 ms; open 2,500 only got 1,566 req/s with
p99 8.8 s).

What it shows:

- Below capacity (F1) everything keeps up and latency stays low.
- Above capacity (F2) the server can't keep up, requests queue, and latency climbs into
  seconds. The closed loop at the same moment said p99 was under 90 ms. Both are
  "true", but only the open loop tells you what users would actually feel.
- The limit check itself (from `Server-Timing`) stays under 2 ms even in F2. The time
  is going to queueing in front of the app, not to Redis or the Lua scripts.
- No run let through more than the limit allows.

One thing I had to fix to get these numbers: my first version of the open loop made
one task per request, and with a backlog the load tool itself slowed down (asking for
2,000 req/s got fewer than asking for 1,500). Now a fixed pool of workers pulls from a
queue.

## Live site (2026-11-23)

https://rate-limiter-lac.vercel.app. The function runs in Vercel's `iad1` region
(Washington, D.C.) and Redis Cloud (free tier) is in the same region. I kept this one small since it's a free account.

| Clients | Users | Duration | Requests | Req/s | 200 | 429 | Errors | Latency p50 / p95 / p99 | Let through / max |
|---------|-------|----------|----------|-------|-----|-----|--------|-------------------------|-------------------|
| 10 | 50 | 20.3s | 658 | 32.5 | 79 | 579 | 0 | 300 / 350 / 449 ms | 79 / 80 |

Every request came from my laptop's IP, so the per-IP limit (60 per minute) is the one
that kicked in: 60 to start plus 1 per second for 20 seconds is 80.

The ~300 ms latency is almost all network. The `x-vercel-id` header shows my requests
entered Vercel at its Singapore edge (`sin1`) and then went to the function in
Washington (`iad1`). To check how much the limiter itself costs, I had the server time
its own limit check and send it back in a `Server-Timing` header. Over 40 requests:

| | min | p50 | p90 | max |
|-|-----|-----|-----|-----|
| limit check inside the server (2 Redis script calls) | 3.0 ms | 3.3 ms | 4.2 ms | 45 ms |

The 45 ms one was probably a request that had to open a new connection to Redis (with
a TLS handshake); the rest reused an open one.

### After batching the two limits (2026-11-24)

Later I changed the service to check the IP and user limits in one Lua script instead
of two (see "One round trip for both limits" in the README). Measured the same way:

| | requests | min | p50 | p90 | max |
|-|----------|-----|-----|-----|-----|
| user + IP limit, one script | 40 | 0.88 ms | 0.99 ms | 1.56 ms | 5.7 ms |
| IP limit only (no user header), one script | 18 | 0.85 ms | 0.95 ms | 2.58 ms | 3.2 ms |

Checking both limits now costs about the same as checking one. I can't compare these
directly to the 3.3 ms from the day before, because the network between Vercel and
Redis Cloud was faster this time: a plain `PING` from `/api/ready` took 0.6 ms. The
fair comparison is the second row, measured right after the first one. One script is
about 1 ms, so the old way (two scripts back to back) would be about 2 ms today.

I also sent 20 requests at the same moment as one user, a couple of times. One run let
10 through and the other let 11 through, and the rest got 429, even though Vercel can
spread requests over more than one server instance. The bucket holds 10, and when the
20 requests take a bit over a second to all land, 1 more token refills in that time, so
11 is correct too.

### Again on 0.2.0 (2026-11-25)

Same run as the first live one, after all the 0.2.0 changes (batched checks, breaker,
fallback, capped connection pool):

| Clients | Users | Duration | Requests | Req/s | 200 | 429 | Errors | Latency p50 / p95 / p99 | Limit check p50 / p95 / p99 | Let through / max |
|---------|-------|----------|----------|-------|-----|-----|--------|-------------------------|-----------------------------|-------------------|
| 10 | 50 | 20.3s | 635 | 31.3 | 79 | 556 | 0 | 297 / 386 / 718 ms | 1.5 / 2.8 / 58 ms | 79 / 80 |

Same throughput, same 0 errors, same 79 of a possible 80. The limit check column comes
from the new `Server-Timing` collection in the load tool. Its p99 of 58 ms is the few
requests that landed on a fresh Vercel instance and had to open a TLS connection to
Redis first; the p95 is under 3 ms.

The 20-at-once burst, twice, 25 seconds apart (as two different users): 10 through and
10 blocked both times.

## Running them yourself

```bash
# a throwaway Redis
redis-server --port 6399 --save "" --appendonly no

# run A: in memory, 1 process
RATE_LIMIT_BACKEND=memory USER_LIMIT=10/10s IP_LIMIT=1000000/1s \
  uv run uvicorn webapp.main:app --port 8100 --workers 1 --no-access-log
uv run python -m loadtest.loadtest --url http://127.0.0.1:8100/api/hello \
  --duration 20 --concurrency 50 --users 100 --limit 10/10s

# runs B and C: Redis, 4 processes
RATE_LIMIT_BACKEND=redis REDIS_URL=redis://127.0.0.1:6399/0 USER_LIMIT=10/10s \
  IP_LIMIT=1000000/1s uv run uvicorn webapp.main:app --port 8101 --workers 4 --no-access-log
uv run python -m loadtest.loadtest --url http://127.0.0.1:8101/api/hello \
  --duration 20 --concurrency 100 --users 1 --limit 10/10s      # B
uv run python -m loadtest.loadtest --url http://127.0.0.1:8101/api/hello \
  --duration 20 --concurrency 100 --users 1000 --limit 10/10s   # C

# runs E and F: same server as B/C
uv run python -m loadtest.loadtest --url http://127.0.0.1:8101/api/hello \
  --duration 15 --concurrency 100 --users 1000 --limit 10/10s                # E
uv run python -m loadtest.loadtest --url http://127.0.0.1:8101/api/hello \
  --duration 15 --rate 1500 --concurrency 100 --users 1000 --limit 10/10s    # F1 (2500 for F2)

# live site: one per-IP limit covers everything, so --limit-keys 1
uv run python -m loadtest.loadtest --url https://rate-limiter-lac.vercel.app/api/hello \
  --duration 20 --concurrency 10 --users 50 --limit 60/1m --limit-keys 1
```
