# Benchmarks

What one limit check costs, measured with [`bench/bench.py`](../bench/bench.py). Raw
output is in [`docs/bench/`](bench/). The load tests in [LOAD_TEST.md](LOAD_TEST.md)
measure the whole HTTP request; this is just the limiter.

Machine: Apple M5 laptop, Python 3.11, Redis 8 running locally on the same laptop.
Each number is the median of 5 runs, in microseconds per check.

## In memory

200,000 checks per run, nothing blocked (the limit is huge), so it's timing the normal
"allowed" path.

| Algorithm | One hot key | Spread over 100k keys |
|-----------|-------------|-----------------------|
| counter | 0.57 µs | 0.62 µs |
| fixed window | 0.64 µs | 0.70 µs |
| sliding window (log) | 0.69 µs | 0.82 µs |
| sliding counter | 0.92 µs | 0.96 µs |
| token bucket | 0.88 µs | 1.04 µs |

All of them are around a microsecond, so a single Python process could do roughly a
million checks a second before the limiter itself was the bottleneck. In practice the
web framework costs far more per request than this. The sliding counter and token
bucket are a bit slower because they do float math on every check (weights, refill)
where the others mostly compare integers. Spreading over 100k keys costs a little more
from the bigger dict and the auto-sweep running now and then.

## Redis

5,000 calls per run, local Redis, so this is mostly the cost of a round trip on one
machine.

| | per check |
|-|-----------|
| `PING` (just the round trip) | 59 µs |
| one limit, one script | 69 µs |
| two limits, one script each | 137 µs |
| two limits, batched into one script | 78 µs |

Running the Lua itself costs about 10 µs on top of the round trip. Checking the IP
and the user limit one after the other is two round trips; batching them is one round
trip plus a little more Lua, so it's 43% less time here. Over a real network the round
trip is a bigger share of the total, so the saving grows. Live on Vercel one script
takes about 1 ms, so the batched check is about 1 ms where two scripts would be about
2 ms (see LOAD_TEST.md).

## Running it

```bash
make redis   # throwaway Redis on 6399
uv run python -m bench.bench --redis redis://127.0.0.1:6399/0
```
