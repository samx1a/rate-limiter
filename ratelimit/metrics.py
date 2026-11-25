"""Tiny in-process metrics: labelled counters and one latency histogram.

Not a replacement for a real metrics library. It's here so the service can answer
"how many requests did each policy block?" and "how long do limit checks take?"
without a new dependency, and it renders the Prometheus text format so a real
scraper can read it.

Counts are per process. On a platform that runs many short-lived instances (like
Vercel) each instance has its own numbers; you'd push them to a metrics backend
instead of scraping.
"""

from __future__ import annotations

import bisect
import threading
from collections import Counter
from collections.abc import Callable

# Upper bounds in milliseconds. A limit check is a few ms with Redis close by;
# anything past 250 ms means the store is timing out.
LATENCY_BUCKETS_MS = (0.5, 1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1000)

Labels = tuple[tuple[str, str], ...]


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[tuple[str, Labels]] = Counter()
        self._buckets = [0] * (len(LATENCY_BUCKETS_MS) + 1)  # last one is +Inf
        self._latency_sum_ms = 0.0
        self._gauges: dict[str, Callable[[], float]] = {}

    def inc(self, name: str, amount: int = 1, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] += amount

    def gauge(self, name: str, read: Callable[[], float]) -> None:
        """A value read fresh at render time, e.g. whether the breaker is open."""
        with self._lock:
            self._gauges[name] = read

    def observe_latency(self, ms: float) -> None:
        index = bisect.bisect_left(LATENCY_BUCKETS_MS, ms)
        with self._lock:
            self._buckets[index] += 1
            self._latency_sum_ms += ms

    def value(self, name: str, **labels: str) -> int:
        with self._lock:
            return self._counters[(name, tuple(sorted(labels.items())))]

    def render(self) -> str:
        """Prometheus text exposition format."""
        with self._lock:
            counters = sorted(self._counters.items())
            buckets = list(self._buckets)
            total_ms = self._latency_sum_ms
            gauges = sorted(self._gauges.items())
        lines: list[str] = []
        for name, read in gauges:
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {read():g}")
        seen: set[str] = set()
        for (name, labels), count in counters:
            if name not in seen:
                lines.append(f"# TYPE {name} counter")
                seen.add(name)
            lines.append(f"{name}{_labels(labels)} {count}")
        name = "ratelimit_check_duration_ms"
        lines.append(f"# TYPE {name} histogram")
        cumulative = 0
        for bound, count in zip([*LATENCY_BUCKETS_MS, "+Inf"], buckets, strict=True):
            cumulative += count
            lines.append(f'{name}_bucket{{le="{bound}"}} {cumulative}')
        lines.append(f"{name}_sum {total_ms:.3f}")
        lines.append(f"{name}_count {cumulative}")
        return "\n".join(lines) + "\n"


def _labels(labels: Labels) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
