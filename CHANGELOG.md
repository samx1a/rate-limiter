# Changelog

## 0.2.0 (2026-11-25)

New:

- **Sliding window counter** (`sliding_counter`): two numbers per key instead of one
  entry per request, for big limits. In Python and Lua, on the demo page, in every test.
- **One Redis round trip for both limits.** The IP and user limits are checked in one
  Lua script, all or nothing, so a 429 from one limit no longer spends the other.
  `REDIS_BATCH=false` turns it off for Redis Cluster.
- **Circuit breaker** in front of Redis (`BREAKER_THRESHOLD`, `BREAKER_COOLDOWN_MS`),
  so an outage costs ~0 ms per request instead of a 250 ms timeout each.
- **Local fallback** (`LOCAL_FALLBACK`, on by default): while Redis is down each server
  limits from memory instead of letting everything through.
- **Expensive endpoints** can count as several requests (`/api/report` costs 5).
- `IP_ALLOWLIST`, `REDIS_MAX_CONNECTIONS`, and clear startup errors for bad settings.
- `/api/ready` (pings Redis, shows the breaker), `/api/metrics` (Prometheus format).
- `reset(key)` on every limiter.
- Load test `--rate` (open loop) and server-side check times; `bench/` micro-benchmarks;
  `examples/client.py`, a client that handles 429s properly.
- Dockerfile, `compose.yaml` (two servers sharing one Redis), Makefile, pre-commit,
  Dependabot, MIT license.

Changed:

- IPv6 clients are limited per /64 network instead of per address.
- In-memory limiters drop idle keys on their own (amortized O(1), no thread).
- Redis outages are logged once when they start and once when they end.
- Strict Content-Security-Policy on the demo page; nosniff and no-referrer everywhere.
- The 429 and 503 responses are documented in the OpenAPI schema.

Fixed:

- Fixed window Lua script handed out a fresh window if Redis's clock stepped back.
- Sliding log could block a request that waited exactly the `retry_after` it was told,
  from float rounding (found by the property tests).
- The README claimed exactly 10 of 20 parallel requests get through live; it's 10 or
  11, because the bucket refills while they arrive.

Testing:

- Property-based tests (Hypothesis) for every algorithm's guarantee, and a fuzz test
  that runs the Python and Lua versions side by side.
- CI on Python 3.11 to 3.14, Redis 6/7/8, free-threaded 3.14t, coverage floor of 90%,
  and a Docker job that checks two containers share one limit.

## 0.1.0 (2026-11-23)

First version: counter, fixed window, sliding log and token bucket in memory and as
Redis Lua scripts, a service with per-IP and per-user limits, FastAPI with 429s and
`RateLimit-*` headers, load tests, CI, and the live demo.
