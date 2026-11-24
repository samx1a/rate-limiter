import logging

import pytest

from ratelimit import Decision, FakeClock
from ratelimit.algorithms.token_bucket import TokenBucket
from ratelimit.service import (
    Identity,
    LimiterUnavailable,
    Policy,
    RateLimitService,
    Scope,
    Verdict,
)


class BrokenLimiter:
    limit = 1

    def hit(self, key: str, cost: int = 1) -> Decision:
        raise LimiterUnavailable("store down")

    def reset(self, key: str) -> None:
        raise LimiterUnavailable("store down")


class RecordingLimiter:
    """Wraps a limiter and records the keys it was asked about."""

    def __init__(self, inner: TokenBucket) -> None:
        self.inner = inner
        self.keys: list[str] = []

    @property
    def limit(self) -> int:
        return self.inner.limit

    def hit(self, key: str, cost: int = 1) -> Decision:
        self.keys.append(key)
        return self.inner.hit(key, cost)

    def reset(self, key: str) -> None:
        self.inner.reset(key)


def service(
    ip_limit: int = 5, user_limit: int = 3
) -> tuple[RateLimitService, RecordingLimiter, RecordingLimiter]:
    clock = FakeClock()
    ip = RecordingLimiter(TokenBucket(ip_limit, 1.0, clock))
    user = RecordingLimiter(TokenBucket(user_limit, 1.0, clock))
    svc = RateLimitService(
        [  # user listed first on purpose: the service must still check IP first
            Policy("user", Scope.USER, user, period=10),
            Policy("ip", Scope.IP, ip, period=10),
        ]
    )
    return svc, ip, user


def test_checks_ip_then_user_with_separate_keys() -> None:
    svc, ip, user = service()
    verdict = svc.check(Identity(ip="1.2.3.4", user="alice"))
    assert verdict.allowed
    assert [r.policy.name for r in verdict.results] == ["ip", "user"]
    assert ip.keys == ["ip:ip:1.2.3.4"]
    assert user.keys == ["user:user:alice"]


def test_user_limit_blocks_and_is_the_binding_result() -> None:
    svc, _, _ = service(ip_limit=10, user_limit=2)
    who = Identity(ip="1.2.3.4", user="alice")
    assert svc.check(who).allowed and svc.check(who).allowed
    blocked = svc.check(who)
    assert not blocked.allowed
    assert blocked.binding is not None and blocked.binding.policy.name == "user"


def test_users_behind_one_ip_share_the_ip_limit() -> None:
    svc, _, _ = service(ip_limit=3, user_limit=10)
    results = [svc.check(Identity(ip="9.9.9.9", user=f"u{n}")).allowed for n in range(4)]
    assert results == [True, True, True, False]


def test_blocked_ip_does_not_spend_user_quota() -> None:
    svc, _, user = service(ip_limit=1, user_limit=5)
    svc.check(Identity(ip="1.1.1.1", user="alice"))
    for _ in range(10):
        assert not svc.check(Identity(ip="1.1.1.1", user="alice")).allowed
    assert user.keys == ["user:user:alice"]  # only the first, allowed request
    assert svc.check(Identity(ip="2.2.2.2", user="alice")).results[-1].decision.remaining == 3


def test_anonymous_requests_only_get_ip_limits() -> None:
    svc, _, user = service()
    verdict = svc.check(Identity(ip="1.2.3.4"))
    assert verdict.allowed and [r.policy.name for r in verdict.results] == ["ip"]
    assert user.keys == []


def test_binding_is_the_policy_closest_to_blocking() -> None:
    svc, _, _ = service(ip_limit=10, user_limit=3)
    verdict = svc.check(Identity(ip="1.2.3.4", user="alice"))
    assert verdict.binding is not None
    assert verdict.binding.policy.name == "user"  # 2 left vs 9 left
    assert Verdict(True, ()).binding is None


def test_ipv6_subjects_are_safe_in_keys() -> None:
    svc, ip, _ = service()
    svc.check(Identity(ip="2001:db8::1"))
    assert ip.keys == ["ip:ip:2001:db8::1"]


def test_fail_open_allows_and_marks_degraded() -> None:
    svc = RateLimitService([Policy("ip", Scope.IP, BrokenLimiter(), period=1)])
    verdict = svc.check(Identity(ip="1.2.3.4"))
    assert verdict.allowed and verdict.degraded


def test_fail_closed_raises() -> None:
    svc = RateLimitService([Policy("ip", Scope.IP, BrokenLimiter(), period=1)], fail_open=False)
    with pytest.raises(LimiterUnavailable):
        svc.check(Identity(ip="1.2.3.4"))


def test_fallback_limits_while_the_store_is_down() -> None:
    fallback = RecordingLimiter(TokenBucket(2, 1.0, FakeClock()))
    svc = RateLimitService(
        [Policy("ip", Scope.IP, BrokenLimiter(), period=2, fallback=fallback)],
        fail_open=False,  # never reached: the fallback answers instead
    )
    verdicts = [svc.check(Identity(ip="1.2.3.4")) for _ in range(3)]
    assert [v.allowed for v in verdicts] == [True, True, False]
    assert all(v.degraded for v in verdicts)
    assert fallback.keys == ["ip:ip:1.2.3.4"] * 3  # same key as the store would use
    assert verdicts[-1].binding is not None and verdicts[-1].binding.decision.remaining == 0


def test_a_healthy_store_never_touches_the_fallback() -> None:
    fallback = RecordingLimiter(TokenBucket(2, 1.0, FakeClock()))
    svc = RateLimitService(
        [Policy("ip", Scope.IP, TokenBucket(5, 1.0, FakeClock()), period=5, fallback=fallback)]
    )
    verdict = svc.check(Identity(ip="1.2.3.4"))
    assert verdict.allowed and not verdict.degraded
    assert fallback.keys == []


def test_policy_names_must_be_unique() -> None:
    limiter = TokenBucket(1, 1.0)
    with pytest.raises(ValueError):
        RateLimitService(
            [Policy("a", Scope.IP, limiter, period=1), Policy("a", Scope.USER, limiter, period=1)]
        )


class SwitchableLimiter(RecordingLimiter):
    def __init__(self) -> None:
        super().__init__(TokenBucket(100, 1.0, FakeClock()))
        self.up = True

    def hit(self, key: str, cost: int = 1) -> Decision:
        if not self.up:
            raise LimiterUnavailable("store down")
        return super().hit(key, cost)


def test_an_outage_is_logged_once_when_it_starts_and_once_when_it_ends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = SwitchableLimiter()
    svc = RateLimitService([Policy("ip", Scope.IP, store, period=1)])
    who = Identity(ip="1.2.3.4")
    with caplog.at_level(logging.WARNING, logger="ratelimit.service"):
        svc.check(who)
        store.up = False
        for _ in range(50):
            svc.check(who)
        store.up = True
        for _ in range(50):
            svc.check(who)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "unavailable (store down); failing open" in messages[0]
    assert "is back" in messages[1]
