"""Replay scripted traffic through the real in-memory limiters on a fake clock.

Used by the demo page to show, side by side, how each algorithm treats the same
requests. Runs instantly (no sleeping) and keeps no state between calls.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from enum import StrEnum

from ratelimit.clock import FakeClock
from ratelimit.limits import Algorithm, LimitSpec, build_limiter

SPEC = LimitSpec(5, 10)  # 5 requests per 10 seconds
# The edge burst is about how much gets through in a *short* span, so the demo reports
# the most admitted in any 2 seconds. (A token bucket legitimately admits more than 5
# in some 10s spans: its promise is "burst of 5, then 0.5/s", not "5 per any 10s".)
BURST_SPAN = 2.0
COMPARED = (
    Algorithm.FIXED_WINDOW,
    Algorithm.SLIDING_WINDOW,
    Algorithm.SLIDING_COUNTER,
    Algorithm.TOKEN_BUCKET,
)


class Scenario(StrEnum):
    EDGE_BURST = "edge_burst"
    STEADY = "steady"


def _times(scenario: Scenario) -> list[float]:
    if scenario is Scenario.EDGE_BURST:
        # 5 just before a window edge, 5 just after, then 5 more a few seconds later.
        before_edge = [9.0, 9.2, 9.4, 9.6, 9.8]
        after_edge = [10.0, 10.2, 10.4, 10.6, 10.8]
        later = [15.0, 15.2, 15.4, 15.6, 15.8]
        return [*before_edge, *after_edge, *later]
    # Two requests per second for 30 seconds: 6x the allowed rate.
    return [n * 0.5 for n in range(60)]


@dataclass(frozen=True, slots=True)
class Outcome:
    t: float
    allowed: bool


def max_in_span(times: list[float], span: float) -> int:
    """The most admitted requests inside any ``span``-second stretch of time."""
    best = 0
    for i, start in enumerate(times):
        count = sum(1 for t in times[i:] if t < start + span)
        best = max(best, count)
    return best


@functools.cache
def simulate(scenario: Scenario) -> dict[str, object]:
    """Cached: the result only depends on the scenario. Callers must not mutate it."""
    times = _times(scenario)
    rows = []
    for algorithm in COMPARED:
        clock = FakeClock()
        limiter = build_limiter(algorithm, SPEC, clock)
        outcomes = []
        for t in times:
            clock.set(t)
            outcomes.append(Outcome(t, limiter.hit("demo").allowed))
        admitted = [o.t for o in outcomes if o.allowed]
        rows.append(
            {
                "algorithm": str(algorithm),
                "outcomes": [{"t": o.t, "allowed": o.allowed} for o in outcomes],
                "admitted": len(admitted),
                "max_in_burst_span": max_in_span(admitted, BURST_SPAN),
            }
        )
    return {
        "scenario": str(scenario),
        "limit": SPEC.requests,
        "period": SPEC.period,
        "burst_span": BURST_SPAN,
        "duration": max(times) + 1,
        "rows": rows,
    }
