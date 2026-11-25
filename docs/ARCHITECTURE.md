# How it fits together

This is the map I wish I'd had when I came back to the code after a few days.

## One request, start to finish

```
client
  │  GET /api/hello   X-User-Id: alice
  ▼
webapp/main.py ── identify ──► webapp/identity.py
  │                              real IP (socket, or TRUSTED_IP_HEADER)
  │                              IPv6 -> its /64, user id validated
  │                              allowlisted? skip everything below
  ▼
rate_limit dependency (weight 1, or 5 for /api/report)
  │
  ▼
ratelimit/service.py   RateLimitService.check(identity, weight)
  │  builds the checks: [ip policy, user policy] (user skipped if anonymous)
  │
  ├─ Redis + REDIS_BATCH ──► redis_limiters.hit_many ──► one Lua script, both keys,
  │                                                       all or nothing
  ├─ otherwise ───────────► policy.limiter.hit(key, cost) one at a time,
  │                         stop at the first block
  │
  │  Redis unreachable or breaker open -> LimiterUnavailable
  │     └─ policy.fallback (in-memory twin) decides, verdict marked degraded
  │     └─ no fallback: FAIL_OPEN allows, otherwise 503
  ▼
Verdict (allowed?, one Decision per policy checked)
  │
  ▼
webapp/headers.py   RateLimit-Limit / -Remaining / -Reset / -Policy, Retry-After
  │                 from the "binding" policy: the one that blocked, or else the
  │                 one closest to blocking
  ▼
200 with headers, or 429 with headers + JSON body
```

## The layers, and what each one doesn't know

| Layer | Knows about | Doesn't know about |
|-------|-------------|--------------------|
| `ratelimit/algorithms/` | one key, a limit, a clock | HTTP, users, Redis |
| `ratelimit/lua/` + `redis_limiters.py` | the same, with the state in Redis | HTTP, users |
| `ratelimit/service.py` | policies (IP, user), fallbacks, failing open | HTTP, headers |
| `webapp/` | HTTP, headers, who the client is, settings | how any algorithm works |

Keeping those lines is what makes the tests easy. The algorithms are tested with a
fake clock and no network. The service is tested with fake limiters that break on
command. Only `webapp/` tests need an HTTP client.

## Where the state lives

- **Memory backend:** a dict per limiter, behind one lock per limiter. Idle keys get
  swept out whenever the dict doubles in size. Only correct with one server process.
- **Redis backend:** one key per (algorithm, policy, subject), e.g.
  `rl:tb:user:user:alice`. Every key has an expiry, so idle users disappear on their
  own. Every check is one Lua script, which Redis runs start to finish with nothing in
  between, so many servers can share a key safely.

## One algorithm, two languages

Each algorithm exists in Python (`ratelimit/algorithms/*.py`) and in Lua
(`ratelimit/lua/*.lua`). The Lua files only define `decide()`; Python glues on
`_prelude.lua` and either `_single.lua` (one key) or `_multi.lua` (several keys, all or
nothing) before loading the script.

Keeping the two in sync is the risky part, so three kinds of tests compare them:
`test_contract.py` (same fixed scenarios on both), `test_differential.py` (Hypothesis
throws random traffic at both and every decision must match), and the per-backend
tests.

## When things go wrong

| What breaks | What happens |
|-------------|--------------|
| Redis slow | each call times out after `REDIS_TIMEOUT_MS` (250 ms) |
| Redis down | after `BREAKER_THRESHOLD` (3) failures in a row the breaker opens and calls fail instantly; every `BREAKER_COOLDOWN_MS` (5 s) one request tries Redis again |
| ...meanwhile | each server limits from memory (`LOCAL_FALLBACK`), so up to N× the limit across N servers; or fails open; or returns 503 |
| too many connections | each server's pool is capped (`REDIS_MAX_CONNECTIONS`); a request waits up to the timeout for a free one |
| a script bug | a real error, not "Redis is down": it propagates as a 500 so it gets noticed |

`/api/ready` shows whether Redis answers a ping and what state the breaker is in.
`/api/metrics` counts fallbacks, fail-opens and 503s per policy.

## Things that are deliberately not here

- **Async Redis.** The limit check is a sync dependency, so FastAPI runs it in a
  worker thread. An async client would save the thread hop, but redis-py's async
  connections are tied to one event loop, and serverless runtimes don't promise to
  keep the same loop between requests. At ~1 ms per check live it wasn't worth that
  risk.
- **Redis Cluster for batching.** The batched script touches the IP key and the user
  key, which would usually be on different nodes. `REDIS_BATCH=false` works on
  Cluster; batching doesn't.
- **Real auth.** The user id is a header so the demo is easy to poke at. In a real
  app it would come from a verified session or token.
