# rate-limiter

[![CI](https://github.com/samx1a/rate-limiter/actions/workflows/ci.yml/badge.svg)](https://github.com/samx1a/rate-limiter/actions/workflows/ci.yml)

I wanted to actually understand how rate limiting works instead of just installing a
library, so I built one up step by step. It starts as a dumb counter and ends up as a
FastAPI service that keeps its state in Redis, where every check is a single Lua script
so the limit still holds when there are multiple servers.

**Live demo:** https://rate-limiter-lac.vercel.app
(FastAPI on Vercel, state in Redis Cloud, both in Washington, D.C.)

The demo lets you spam requests until you get a 429, and it also replays the same
traffic through four algorithms side by side so you can see where they differ.

## What's in here

```
ratelimit/
  algorithms/        in-memory limiters, each one fixes a problem with the one before
    counter.py         counts per key forever. just the baseline
    fixed_window.py    resets every N seconds. cheap, but leaks bursts at the edges
    sliding_window.py  looks back exactly N seconds from now
    sliding_counter.py same idea with two numbers per key instead of a log
    token_bucket.py    bursts up to a capacity, then refills at a steady rate
  lua/               the same four time-based algorithms as Redis Lua scripts
  redis_limiters.py  Python side of the Redis version
  service.py         applies a per-IP limit and a per-user limit to each request
webapp/              FastAPI app, 429s + headers, the demo page
loadtest/            load test tool (results in docs/LOAD_TEST.md)
tests/               195 tests
PROMPT.md            the spec and checklist I wrote before starting
```

## The fixed window edge problem

Fixed window splits time into back-to-back windows and counts requests per window. The
catch is that the limit is per *calendar* window, not per any 60 seconds. With 100
requests per minute:

- at 0:59.9 send 100 requests, all allowed (that window isn't full yet)
- at 1:00.0 send 100 more, all allowed (new window, count is back to 0)

That's 200 requests in a tenth of a second, double the limit, and you can do it every
minute. `test_edge_burst_lets_through_twice_the_limit` shows it happening. The sliding
window and token bucket don't have this problem, which is why they exist.

## Token bucket without a background thread

Each key stores two numbers: how many tokens it has and when it last refilled. When a
request comes in, I add `elapsed seconds * refill rate` tokens (capped at the capacity)
and then decide. Nothing runs in between requests, so an idle key costs nothing.

One thing that bit me: adding 0.1 ten times gives `0.9999999999999999`, which would block
a request that should go through. Comparisons use a tiny tolerance for that, and there's
a test that fails without it.

## Thread safety

Each limiter has one lock, held for the whole read, decide, write. The clock is read
inside the lock too, otherwise a thread that grabbed the time earlier could write after
a later one and the sliding window's log would end up out of order.

To test it, 32 threads hit one key 200 times each, with Python's thread switch interval
turned down to 1 microsecond so they interleave as much as possible. Exactly the limit
gets through every time. I also swapped the lock for a no-op to make sure the tests can
actually catch a race: 4 or 5 of them fail on every run without it.

## Forgetting idle keys

At first the in-memory limiters remembered every key they'd ever seen, so one request
from each of a million IPs meant a million entries that never went away. Now each
algorithm knows when a key's state is the same as having no state at all (token bucket
refilled to full, window over, log empty), and those get swept out.

There's no background thread for this either. The sweep runs inside `hit()` whenever
the map has doubled in size since the last sweep, the same trick a growable array
uses, so the cost averages out to O(1) per request. A test pushes 100,000 one-off
clients through and the map never goes past about 2,000 keys.

## The Redis part

With more than one server, each one keeping its own count in memory doesn't work. If
every server does `GET`, decides, then `SET`, two servers can read the same old value at
the same time and both let a request through. So each algorithm is a Lua script that
does the read, the decision and the write in one go. Redis runs a script start to finish
before doing anything else, so nothing can sneak in between.

Some details:

- Scripts run with `EVALSHA` and reload automatically if Redis loses its script cache.
- They use the Redis server's clock (`TIME`) by default, so app servers with slightly
  different clocks still agree on what "now" is. Tests pass their own time instead.
- Every key gets an expiry, so a user who stops sending requests disappears from Redis
  on their own. After a load test with 1,000 users, all 1,000 keys were gone about 11
  seconds later.
- The sliding window gives each request a unique id. Otherwise two requests in the same
  millisecond would count as one.
- To prove it across processes, `test_distributed.py` starts 8 separate Python
  processes (own memory, own connection) that all hammer one key. Together they get
  exactly the limit through. I ran it against the cloud Redis too, not just locally.

**If Redis goes down:** by default the service fails open, meaning it logs the problem
and lets requests through without limiting. A rate limiter taking the whole site down
seemed worse than briefly not limiting. Set `FAIL_OPEN=false` to return 503 instead.
Redis commands time out after 250 ms, so an outage doesn't make every request hang.

## Using the API

`GET /api/hello` is rate limited per IP (60 per minute) and per user (10 per 10
seconds, token bucket). The user comes from an `X-User-Id` header. In a real app that
would come from a login token, but a header keeps the demo simple.

Here's a real response from the live site after using up a user's limit:

```
HTTP/2 429
ratelimit-limit: 10
ratelimit-remaining: 0
ratelimit-reset: 10
ratelimit-policy: 60;w=60;name="ip", 10;w=10;name="user"
retry-after: 1

{"detail":{"error":"rate_limited","message":"Too many requests. Slow down and retry later.","policy":"user","retry_after":1}}
```

The `RateLimit-*` headers follow the IETF draft. They always describe whichever limit
you're closest to hitting. Successful responses get them too, so a client can slow down
before it gets blocked.

For the IP, the app uses the socket address unless you tell it to trust a proxy header.
On Vercel I set `TRUSTED_IP_HEADER=x-real-ip`, since Vercel overwrites that header
itself. Sending a fake `X-Forwarded-For` to the live site doesn't change your IP (I
checked).

## Does it actually work

- **Tests:** 195 tests. CI runs them on Python 3.11, 3.12 and 3.13 against Redis 7, plus
  Redis 6, along with ruff and mypy in strict mode. In CI the Redis tests fail instead of
  skipping if Redis isn't there, so they can't quietly not run.
- **Same behavior on both backends:** `test_contract.py` runs one set of checks against
  every algorithm in memory and in Redis.
- **Load tests** (full numbers in [docs/LOAD_TEST.md](docs/LOAD_TEST.md)):

  | Run | Req/s | Errors | Let through / max allowed |
  |-----|-------|--------|---------------------------|
  | In memory, 1 process, 100 users | 3,305 | 0 | 2,900 / 3,000 |
  | Redis, 4 processes, 1 hot user | 2,460 | 0 | 29 / 30 |
  | Redis, 4 processes, 1,000 users | 2,490 | 0 | 29,000 / 30,000 |
  | Live site, 10 clients | 32.5 | 0 | 79 / 80 |

- **Live:** 20 requests sent at the same moment as one user get 10 or 11 through and
  the rest blocked. 10 is the bucket size; the 11th happens when the requests take
  more than a second to all arrive, because the bucket refills 1 token per second in
  the meantime. That's the limiter working, not leaking. Inside the server, the limit
  check (two Redis calls) takes about 3 ms (it reports this in a `Server-Timing`
  header).

## Running it

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). Redis is optional.

```bash
uv sync --extra dev
uv run uvicorn webapp.main:app --reload        # http://127.0.0.1:8000, in-memory
uv run pytest                                  # Redis tests skip if no Redis

# with Redis
redis-server --port 6399 --save ""
REDIS_URL=redis://127.0.0.1:6399/0 uv run uvicorn webapp.main:app --reload
REDIS_URL=redis://127.0.0.1:6399/0 uv run pytest
```

| Setting | Default | What it does |
|---------|---------|--------------|
| `RATE_LIMIT_BACKEND` | `redis` if `REDIS_URL` is set, else `memory` | where the counts live |
| `REDIS_URL` | none | Redis connection string (`rediss://` for TLS) |
| `RATE_LIMIT_ALGORITHM` | `token_bucket` | `fixed_window`, `sliding_window`, `sliding_counter` or `token_bucket` |
| `IP_LIMIT` | `60/1m` | per-IP limit, written like `10/s`, `100/1m`, `5/30s` |
| `USER_LIMIT` | `10/10s` | per-user limit |
| `TRUSTED_IP_HEADER` | none | proxy header to read the client IP from (`x-real-ip` on Vercel) |
| `FAIL_OPEN` | `true` | allow requests when Redis is unreachable |
| `REDIS_TIMEOUT_MS` | `250` | per-command Redis timeout |
| `RATE_LIMIT_PREFIX` | `rl` | prefix for Redis keys |

## What I'd change at a bigger scale

- **Sliding window memory.** The sliding log stores one entry per allowed request, so a
  limit like 100,000 per hour means up to 100,000 entries per user. That's why I added
  the sliding window counter (`RATE_LIMIT_ALGORITHM=sliding_counter`): two numbers per
  key, and it estimates the last N seconds by weighting the previous window by how much
  of it is still in view. It can be a bit off when traffic is bunched up, but memory
  stays flat no matter how big the limit is.
- **Two Redis calls per request.** The IP and user checks are separate scripts. One
  script that checks both keys would save a round trip and make the pair all or
  nothing. On Redis Cluster both keys would need the same hash slot for that.
- **Hot keys.** One very busy key always lands on one Redis shard. Past that point you'd
  want a small local allowance per server that syncs with Redis now and then, which
  trades a bit of accuracy for far fewer calls.
