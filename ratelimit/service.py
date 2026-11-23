"""Service layer: apply several limits (per IP, per user) to one request.

The web layer turns an HTTP request into an ``Identity`` and asks the service for a
``Verdict``. The service knows nothing about HTTP, and the limiters know nothing about
users or IPs; each only sees opaque keys.

Rules:
- Policies are checked IP first, then user. IP limits are the cheapest defence against
  floods and work for anonymous traffic.
- Checking stops at the first block. A blocked IP therefore never spends a user's
  quota. (Policies checked *before* the block have spent theirs; that is the usual
  trade-off of sequential checks and keeps every check a single atomic step.)
- If a limiter's store is unreachable, ``fail_open`` decides: allow and log (default,
  favours availability) or raise ``LimiterUnavailable`` so the caller can refuse.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ratelimit.decision import Decision, LimiterUnavailable, RateLimiter

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
    def __init__(self, policies: Sequence[Policy], *, fail_open: bool = True) -> None:
        names = [p.name for p in policies]
        if len(names) != len(set(names)):
            raise ValueError("policy names must be unique (they are part of the key)")
        order = {Scope.IP: 0, Scope.USER: 1}
        self._policies = tuple(sorted(policies, key=lambda p: order[p.scope]))
        self._fail_open = fail_open

    @property
    def policies(self) -> tuple[Policy, ...]:
        return self._policies

    @staticmethod
    def key(policy: Policy, subject: str) -> str:
        # The subject goes last, so a ':' inside it (IPv6) cannot cause a collision.
        return f"{policy.name}:{policy.scope}:{subject}"

    def check(self, identity: Identity) -> Verdict:
        results: list[PolicyResult] = []
        for policy in self._policies:
            subject = identity.ip if policy.scope is Scope.IP else identity.user
            if subject is None:
                continue  # anonymous request: user limits do not apply
            try:
                decision = policy.limiter.hit(self.key(policy, subject), policy.cost)
            except LimiterUnavailable:
                if not self._fail_open:
                    raise
                log.warning("rate limiter %r unavailable; failing open", policy.name)
                return Verdict(True, tuple(results), degraded=True)
            results.append(PolicyResult(policy, decision))
            if not decision.allowed:
                return Verdict(False, tuple(results))
        return Verdict(True, tuple(results))
