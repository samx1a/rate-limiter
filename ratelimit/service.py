"""Service layer: apply several limits (per IP, per user) to one request.

The web layer turns an HTTP request into an ``Identity`` and asks the service for a
``Verdict``. The service knows nothing about HTTP, and the limiters know nothing about
users or IPs; each only sees opaque keys.

Rules:
- Policies are checked IP first, then user. IP limits are the cheapest defence against
  floods and work for anonymous traffic.
- Checking stops at the first block. A blocked IP therefore never spends a user's
  quota. With a ``batch`` function (Redis: ``hit_many``) all policies are checked in
  one atomic step and nothing is spent unless all of them allow. Without one, policies
  checked *before* the block have spent theirs.
- If a limiter's store is unreachable and the policy has a ``fallback`` (an in-memory
  limiter), the fallback decides instead. Each server then limits on its own, so with
  N servers a client can get up to N times the limit, but that beats no limit at all.
- With no fallback, ``fail_open`` decides: allow and log (default, favours
  availability) or raise ``LimiterUnavailable`` so the caller can refuse.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ratelimit.decision import Decision, LimiterUnavailable, RateLimiter
from ratelimit.metrics import Metrics

log = logging.getLogger(__name__)

__all__ = [
    "Identity",
    "LimiterUnavailable",
    "Policy",
    "PolicyResult",
    "RateLimitService",
    "Scope",
    "Verdict",
]


# Checks several (limiter, key, cost) at once, all or nothing, stopping at the first
# block. See ratelimit.redis_limiters.hit_many.
BatchHit = Callable[[Sequence[tuple[RateLimiter, str, int]]], list[Decision]]


class Scope(StrEnum):
    IP = "ip"
    USER = "user"


@dataclass(frozen=True, slots=True)
class Policy:
    name: str
    scope: Scope
    limiter: RateLimiter
    period: float  # seconds; describes the policy in response headers
    cost: int = 1
    fallback: RateLimiter | None = None  # used while ``limiter``'s store is down


@dataclass(frozen=True, slots=True)
class Identity:
    ip: str
    user: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyResult:
    policy: Policy
    decision: Decision


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    results: tuple[PolicyResult, ...]
    degraded: bool = False  # True when a store was down and we failed open

    @property
    def binding(self) -> PolicyResult | None:
        """The result clients should see in headers: the policy that blocked, or else
        the one closest to blocking."""
        if not self.results:
            return None
        if not self.allowed:
            return self.results[-1]
        return min(self.results, key=lambda r: (r.decision.remaining, r.decision.limit))


class RateLimitService:
    def __init__(
        self,
        policies: Sequence[Policy],
        *,
        fail_open: bool = True,
        metrics: Metrics | None = None,
        batch: BatchHit | None = None,
    ) -> None:
        names = [p.name for p in policies]
        if len(names) != len(set(names)):
            raise ValueError("policy names must be unique (they are part of the key)")
        order = {Scope.IP: 0, Scope.USER: 1}
        self._policies = tuple(sorted(policies, key=lambda p: order[p.scope]))
        self._fail_open = fail_open
        self.metrics = metrics or Metrics()
        self._batch = batch
        # Policies whose store is currently unreachable. Used to log an outage once
        # when it starts and once when it ends, not on every request in between.
        self._down: set[str] = set()
        self._down_lock = threading.Lock()

    @property
    def policies(self) -> tuple[Policy, ...]:
        return self._policies

    @staticmethod
    def key(policy: Policy, subject: str) -> str:
        # The subject goes last, so a ':' inside it (IPv6) cannot cause a collision.
        return f"{policy.name}:{policy.scope}:{subject}"

    def check(self, identity: Identity, weight: int = 1) -> Verdict:
        """Check every policy that applies to ``identity``.

        ``weight`` scales each policy's cost, so an expensive endpoint can count as
        several requests (``weight=5`` spends 5 tokens where a normal call spends 1).
        """
        if weight < 1:
            raise ValueError("weight must be >= 1")
        checks: list[tuple[Policy, str, int]] = []
        for policy in self._policies:
            subject = identity.ip if policy.scope is Scope.IP else identity.user
            if subject is None:
                continue  # anonymous request: user limits do not apply
            checks.append((policy, self.key(policy, subject), policy.cost * weight))

        if self._batch is not None and len(checks) > 1:
            try:
                decisions = self._batch([(p.limiter, key, cost) for p, key, cost in checks])
            except LimiterUnavailable:
                pass  # check one by one below, which knows each policy's fallback
            else:
                return self._verdict(checks, decisions)
        results: list[PolicyResult] = []
        degraded = False
        for policy, key, cost in checks:
            try:
                decision = policy.limiter.hit(key, cost)
            except LimiterUnavailable as exc:
                self._mark_down(policy, exc)
                if policy.fallback is not None:
                    self.metrics.inc("ratelimit_fallback_total", policy=policy.name)
                    decision = policy.fallback.hit(key, cost)
                    degraded = True
                elif not self._fail_open:
                    self.metrics.inc("ratelimit_store_down_refused_total", policy=policy.name)
                    raise
                else:
                    self.metrics.inc("ratelimit_fail_open_total", policy=policy.name)
                    return Verdict(True, tuple(results), degraded=True)
            else:
                self._mark_up(policy)
            result = "allowed" if decision.allowed else "blocked"
            self.metrics.inc("ratelimit_decisions_total", policy=policy.name, result=result)
            results.append(PolicyResult(policy, decision))
            if not decision.allowed:
                return Verdict(False, tuple(results), degraded=degraded)
        return Verdict(True, tuple(results), degraded=degraded)

    def _mark_down(self, policy: Policy, exc: Exception) -> None:
        with self._down_lock:
            if policy.name in self._down:
                return
            self._down.add(policy.name)
        if policy.fallback is not None:
            action = "limiting from local memory"
        elif self._fail_open:
            action = "failing open"
        else:
            action = "refusing requests"
        log.warning("rate limit store for %r unavailable (%s); %s", policy.name, exc, action)

    def _mark_up(self, policy: Policy) -> None:
        if policy.name not in self._down:  # fast path, no lock: the usual case
            return
        with self._down_lock:
            if policy.name not in self._down:
                return
            self._down.discard(policy.name)
        log.warning("rate limit store for %r is back", policy.name)

    def _verdict(self, checks: list[tuple[Policy, str, int]], decisions: list[Decision]) -> Verdict:
        results = []
        for (policy, _, _), decision in zip(checks, decisions, strict=False):
            self._mark_up(policy)
            result = "allowed" if decision.allowed else "blocked"
            self.metrics.inc("ratelimit_decisions_total", policy=policy.name, result=result)
            results.append(PolicyResult(policy, decision))
        allowed = len(results) == len(checks) and all(r.decision.allowed for r in results)
        return Verdict(allowed, tuple(results))
