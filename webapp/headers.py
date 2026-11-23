"""HTTP rate limit headers.

Follows the IETF "RateLimit header fields" draft (the widely deployed form):

    RateLimit-Limit: 10            requests allowed by the binding policy
    RateLimit-Remaining: 3         requests left right now
    RateLimit-Reset: 7             seconds until the full limit is back
    RateLimit-Policy: 60;w=60;name="ip", 10;w=10;name="user"
    Retry-After: 2                 (only on 429) seconds before retrying

The "binding" policy is the one that blocked, or else the one closest to blocking,
so clients always see the limit they are about to hit.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from ratelimit.service import Policy, Verdict


def whole_seconds(seconds: float) -> int:
    """Round up to whole seconds (headers take integers). A tiny tolerance stops
    float noise such as 2.0000000001 becoming 3."""
    return max(0, math.ceil(seconds - 1e-9))


def policy_header(policies: Sequence[Policy]) -> str:
    return ", ".join(
        f'{p.limiter.limit};w={whole_seconds(p.period)};name="{p.name}"' for p in policies
    )


def rate_limit_headers(verdict: Verdict, policies: Sequence[Policy]) -> dict[str, str]:
    binding = verdict.binding
    if binding is None:
        return {}
    decision = binding.decision
    headers = {
        "RateLimit-Limit": str(decision.limit),
        "RateLimit-Remaining": str(decision.remaining),
        "RateLimit-Policy": policy_header(policies),
    }
    if math.isfinite(decision.reset_after):
        headers["RateLimit-Reset"] = str(whole_seconds(decision.reset_after))
    if not verdict.allowed and math.isfinite(decision.retry_after):
        # At least 1: "retry after 0 seconds" invites an immediate hammering loop.
        headers["Retry-After"] = str(max(1, whole_seconds(decision.retry_after)))
    return headers
