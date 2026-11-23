# Load test results

All numbers below come from real runs of [`loadtest/loadtest.py`](../loadtest/loadtest.py).
Raw JSON for each run is in [`docs/loadtest/`](loadtest/).

**What counts as an error:** a transport failure (timeout, refused connection) or any
status other than 200 and 429. A 429 is the limiter doing its job.

**Correctness check:** every run also checks that the server never let through more than
the limit allows: for a token bucket of `10/10s`, one user may get at most
`10 + 1/s x duration` requests. `over_admitted` must be `false`.

## Local runs (2026-11-23)

Machine: Apple M5 (10 cores), macOS, Python 3.11. The load generator runs on the same
machine as the server and Redis, so these numbers are a floor, not a ceiling: the
single-process Python load generator is the first thing to saturate.

Server config for all local runs: token bucket, `USER_LIMIT=10/10s`,
`IP_LIMIT=1000000/1s` (so the per-user limit is what is being measured, since every
request comes from 127.0.0.1).

| Run | Backend | Server processes | Clients | Users | Duration | Requests | Req/s | 200 | 429 | Errors | p50 / p95 / p99 latency | Admitted / max allowed |
|-----|---------|------------------|---------|-------|----------|----------|-------|-----|-----|--------|-------------------------|------------------------|
| A | in-memory | 1 | 50 | 100 | 20.1s | 66,317 | **3,305** | 2,900 | 63,417 | **0** | 14.1 / 18.7 / 36.0 ms | 2,900 / 3,000 ✅ |
| B | Redis (Lua) | 4 | 100 | 1 (one hot key) | 20.1s | 49,407 | **2,460** | 29 | 49,378 | **0** | 38.4 / 53.0 / 81.8 ms | 29 / 30 ✅ |
| C | Redis (Lua) | 4 | 100 | 1,000 | 20.1s | 50,020 | **2,490** | 29,000 | 21,020 | **0** | 38.7 / 47.8 / 55.8 ms | 29,000 / 30,000 ✅ |

### What these show

- **Run B is the important one.** Four separate server processes, each with its own
  memory and Redis connection, took ~49,000 requests for one user in 20 seconds and
  admitted 29, inside the theoretical maximum of 30 (10 burst + 1 per second x 20s).
  Only the atomic Lua script keeps that total correct across processes.
- **Zero errors** in ~166,000 requests.
- **Redis costs latency, not correctness.** Every request makes one Redis round trip
  (one `EVALSHA`), which is why p50 rises from ~14 ms to ~38 ms here; at 100
  concurrent clients most of that is queueing in the load generator and server, not
  Redis itself.
- **Expiry works.** The 1,000 user buckets from run C disappeared from Redis on their
  own ~11 seconds after the run (time to refill to full + 1s margin).

### How to reproduce

```bash
# terminal 1: a throwaway Redis
redis-server --port 6399 --save "" --appendonly no

# terminal 2: run A (in-memory, 1 process)
RATE_LIMIT_BACKEND=memory USER_LIMIT=10/10s IP_LIMIT=1000000/1s \
  uv run uvicorn webapp.main:app --port 8100 --workers 1 --no-access-log
# terminal 3
uv run python -m loadtest.loadtest --url http://127.0.0.1:8100/api/hello \
  --duration 20 --concurrency 50 --users 100 --limit 10/10s

# runs B and C (Redis, 4 processes)
RATE_LIMIT_BACKEND=redis REDIS_URL=redis://127.0.0.1:6399/0 USER_LIMIT=10/10s \
  IP_LIMIT=1000000/1s uv run uvicorn webapp.main:app --port 8101 --workers 4 --no-access-log
uv run python -m loadtest.loadtest --url http://127.0.0.1:8101/api/hello \
  --duration 20 --concurrency 100 --users 1 --limit 10/10s      # B
uv run python -m loadtest.loadtest --url http://127.0.0.1:8101/api/hello \
  --duration 20 --concurrency 100 --users 1000 --limit 10/10s   # C
```

The tool exits with status 1 if the server ever over-admits.
