# Which algorithm should I use?

Short answer: **token bucket**, unless you have a reason not to. That's why it's the
default. The rest of this page is the reasons.

## Side by side

| | Fixed window | Sliding log | Sliding counter | Token bucket |
|-|--------------|-------------|-----------------|--------------|
| What it promises | ≤ limit per calendar window | ≤ limit in *any* window-long span | ≤ limit per calendar window, estimates the rest | burst of `limit`, then `limit / period` per second |
| Edge burst | **yes, up to 2x** | no | rare, needs bunched traffic; never past 2x | no |
| Memory per key | 2 numbers | 1 entry per allowed request (up to `limit`) | 3 numbers | 2 numbers |
| Exact? | yes, for what it promises | yes | no, an estimate | yes |
| Spreads traffic out | no, clumps at each window start | no, clumps every window | yes | yes |
| Cost per check in memory | 0.64 µs | 0.69 µs | 0.92 µs | 0.88 µs |
| Redis structure | hash | sorted set | hash | hash |

Timings are from [BENCHMARKS.md](BENCHMARKS.md) (one hot key, median of 5 runs). The
"promises" are each checked as properties on random traffic in
`tests/test_properties.py`, so they're not just claims.

## When I'd pick each one

**Token bucket** when you want to allow short bursts but cap the steady rate, which is
most APIs. A user who's been idle can fire off 10 requests at once, then gets one per
second. It also maps cleanly onto "N requests per second" in docs and contracts.

**Sliding log** when the limit is small and has to be exact in every span, like "5
password attempts per 15 minutes". Being off by a few there matters, and storing 5
timestamps per user is nothing. Don't use it for big limits: 100,000 per hour means up
to 100,000 entries per user.

**Sliding counter** for big limits where a small error is fine, like "100,000 requests
per hour per API key". Three numbers per key no matter the limit. It can be a bit off
when traffic is bunched up, but it doesn't have fixed window's built-in 2x edge burst.

**Fixed window** when you need the cheapest possible thing and the edge burst doesn't
matter, or when the limit really is per calendar period, like "1,000 emails per day,
resets at midnight". It's also the easiest to explain to users ("resets on the
minute").

**Counter** (no time at all) only for one-off quotas like "3 free trials". It's here
as the starting point everything else builds on.

## Seeing the difference

The demo page replays the same traffic through all four side by side. The "burst at a
window edge" scenario shows fixed window letting 10 through in 2 seconds against a
limit of 5, while the other three hold it to 5. The steady scenario shows fixed window
and sliding log letting a clump of 5 through every 10 seconds, while sliding counter
and token bucket spread it out to about one every 2 seconds.
