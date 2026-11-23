# Build prompt: production-grade rate limiter

You are a senior backend engineer. Build a rate limiter in Python that goes from a
naive counter to a distributed, Redis-backed service with a FastAPI front end, and
prove that it works. Optimise for **correctness first**, then clarity, then speed.
Every claim in the README must be backed by a test, a measurement, or a link.

## Requirements (each one is a checklist item)

| #  | Requirement | Deliverable | How it is proven |
|----|-------------|-------------|------------------|
| 1  | Dumb counter per key | `ratelimit/algorithms/counter.py` | unit tests |
| 2  | Fixed window, with a note on edge bursts | `fixed_window.py` + docstring and README section | a test that *demonstrates* 2x the limit getting through across a window edge |
| 3  | Sliding window | `sliding_window.py` (sliding log) | a test showing the same edge burst is blocked |
| 4  | Token bucket, lazy refill, no background thread | `token_bucket.py` | refill math tests with a fake clock; no threads created |
| 5  | Thread safe: one lock around check + update | a `threading.Lock` per limiter, held for the whole read-modify-write | stress test: many threads on one key never admit more than capacity |
| 6  | Service layer, wrapped in FastAPI, per user and per IP | `ratelimit/service.py`, `webapp/` | service unit tests + API tests |
| 7  | Blocked requests get 429 with limit headers | `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`, `RateLimit-Policy`, `Retry-After` | API tests assert status and every header |
| 8  | State in Redis | `ratelimit/redis_limiters.py` | tests against a real Redis server |
| 9  | Check and update as ONE atomic Lua script | `ratelimit/lua/*.lua`, run via `EVALSHA` | multi-process test: N processes on one key never exceed capacity |
| 10 | Thread-safe in-memory version for one server | in-memory limiters behind the same interface; chosen by config | same contract tests run against both backends |
| 11 | Unit tests pass | `pytest` | CI green |
| 12 | Load test, with req/s and error counts written down | `loadtest/loadtest.py`, `docs/LOAD_TEST.md` | numbers copied from real runs, with the exact command |
| 13 | Deployed live | Vercel (FastAPI) + managed Redis | live URL in README returns 200 then 429 |
| 14 | CI running the tests | GitHub Actions with a Redis service container | badge + green run |
| 15 | Demo page | `/` served by the app | screenshot-verified; works on phone width |

## Design rules

- **One interface.** Every limiter implements `hit(key, cost=1) -> Decision`.
  `Decision` = `allowed, limit, remaining, reset_after, retry_after` (seconds).
  The service, API and tests depend on the interface, never on a concrete class.
- **Injectable time.** In-memory limiters take a `Clock`; tests use `FakeClock`, so no
  test sleeps. Redis scripts use the **Redis server clock** (`TIME`) in production so
  every app server agrees on "now"; tests may pass an explicit time.
- **Atomicity.** In memory: a single lock held across read, decide and write.
  In Redis: the whole read, decide and write is one Lua script. Never `GET` then `SET`
  from Python.
- **Bounded memory.** Redis keys get a TTL equal to how long their state matters.
  In-memory growth per key is documented (and bounded for the sliding log by `limit`).
- **Exact semantics, written down.** `remaining` never goes negative; `retry_after` is 0
  when allowed; `reset_after` is the time until the full limit is available again.
  Blocked requests do not consume quota.
- **Multiple policies.** A request is checked against per-IP then per-user policies.
  Short-circuit on the first block, so a blocked IP cannot drain a user's quota.
  Response headers describe the most restrictive policy.
- **Failure mode is explicit.** If Redis is unreachable the service fails **open**
  (allow and log) by default, or **closed** (503) when configured. Never hang:
  Redis socket timeouts are short.
- **Trust boundaries.** The client IP comes from the socket unless a trusted proxy
  header is configured (on Vercel, `x-real-ip`). The demo user id comes from a
  validated `X-User-Id` header; in a real system it would come from auth.
- **No magic numbers in code.** Limits come from config strings like `10/10s`.

## Pitfalls to check before every commit

- Integer division and float drift in refill math (use floats, clamp to capacity).
- Off-by-one at window edges (`<` vs `<=`), cost > 1, cost > capacity (always blocked).
- Lua returns floats as integers: return numbers as strings when precision matters.
- Sliding-log members must be unique, or same-millisecond requests collapse.
- `Retry-After` must be a whole number of seconds, rounded **up**, at least 1.
- Tests must not depend on timing luck: use fake clocks and barriers, not sleeps.
- A Redis test that is skipped locally must **fail** in CI (`REQUIRE_REDIS=1`).
- Secrets (Redis URL) only in environment variables, never committed.

## Commit plan (exactly 19, each one builds and passes tests)

1. Build prompt, project skeleton, `Decision` + `Clock`
2. Naive per-key counter with tests
3. Fixed window limiter, with the edge-burst note and a test that proves it
4. Sliding window log limiter
5. Token bucket with lazy refill
6. Thread safety: one lock per limiter + concurrency stress tests
7. Limit-string parsing (`10/10s`) and limiter factory
8. Service layer: per-user and per-IP policies
9. Client identity: user header validation and trusted proxy IP
10. FastAPI app wrapping the service
11. 429 responses and RateLimit headers
12. Redis token bucket as one atomic Lua script
13. Redis fixed and sliding window Lua scripts
14. Backend selection by config (Redis or in-memory) + shared contract tests
15. Multi-process distributed correctness test + Redis failure handling
16. CI: lint, type check, tests against a real Redis
17. Load test tool and recorded results
18. Demo page
19. Live deployment, final README

## Definition of done

All 15 rows above are checked, CI is green, the live URL works, the load-test numbers
in `docs/LOAD_TEST.md` come from real runs, and the README explains the trade-offs a
reviewer would ask about (why sliding log over sliding counter, why server time, why
fail-open, what would change at 100x scale).
