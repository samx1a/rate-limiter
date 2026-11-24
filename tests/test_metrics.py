from ratelimit import FakeClock
from ratelimit.algorithms.token_bucket import TokenBucket
from ratelimit.metrics import Metrics
from ratelimit.service import Identity, Policy, RateLimitService, Scope
from tests.test_service import BrokenLimiter


def test_counters_and_render() -> None:
    m = Metrics()
    m.inc("hits_total", policy="ip", result="allowed")
    m.inc("hits_total", result="allowed", policy="ip")  # label order doesn't matter
    m.inc("hits_total", policy='we"ird')
    assert m.value("hits_total", policy="ip", result="allowed") == 2
    text = m.render()
    assert "# TYPE hits_total counter" in text
    assert 'hits_total{policy="ip",result="allowed"} 2' in text
    assert 'hits_total{policy="we\\"ird"} 1' in text


def test_latency_histogram_is_cumulative() -> None:
    m = Metrics()
    for ms in (0.2, 3, 3, 700, 5000):
        m.observe_latency(ms)
    text = m.render()
    assert 'ratelimit_check_duration_ms_bucket{le="0.5"} 1' in text
    assert 'ratelimit_check_duration_ms_bucket{le="5"} 3' in text
    assert 'ratelimit_check_duration_ms_bucket{le="1000"} 4' in text
    assert 'ratelimit_check_duration_ms_bucket{le="+Inf"} 5' in text
    assert "ratelimit_check_duration_ms_count 5" in text
    assert "ratelimit_check_duration_ms_sum 5706.200" in text


def test_service_counts_decisions_per_policy() -> None:
    clock = FakeClock()
    svc = RateLimitService(
        [
            Policy("ip", Scope.IP, TokenBucket(10, 1.0, clock), period=10),
            Policy("user", Scope.USER, TokenBucket(2, 1.0, clock), period=2),
        ]
    )
    for _ in range(3):
        svc.check(Identity(ip="1.2.3.4", user="alice"))
    m = svc.metrics
    assert m.value("ratelimit_decisions_total", policy="ip", result="allowed") == 3
    assert m.value("ratelimit_decisions_total", policy="user", result="allowed") == 2
    assert m.value("ratelimit_decisions_total", policy="user", result="blocked") == 1


def test_service_counts_outage_handling() -> None:
    fallback = TokenBucket(5, 1.0, FakeClock())
    svc = RateLimitService(
        [
            Policy("ip", Scope.IP, BrokenLimiter(), period=1, fallback=fallback),
            Policy("user", Scope.USER, BrokenLimiter(), period=1),
        ]
    )
    svc.check(Identity(ip="1.2.3.4", user="alice"))
    assert svc.metrics.value("ratelimit_fallback_total", policy="ip") == 1
    assert svc.metrics.value("ratelimit_fail_open_total", policy="user") == 1
