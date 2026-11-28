"""FastAPI wrapper around the rate limiting service.

No ``from __future__ import annotations`` here: FastAPI reads parameter annotations at
runtime, and string annotations cannot see aliases defined inside ``create_app``.
"""

import base64
import hashlib
import hmac
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import redis
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse

from ratelimit import __version__
from ratelimit.algorithms.base import InMemoryLimiter
from ratelimit.breaker import BreakerState, CircuitBreaker
from ratelimit.decision import LimiterUnavailable, RateLimiter
from ratelimit.limits import Algorithm, LimitSpec, build_limiter
from ratelimit.redis_limiters import build_redis_limiter, connect, hit_many
from ratelimit.service import BatchHit, Identity, Policy, RateLimitService, Scope, Verdict
from webapp.headers import rate_limit_headers, whole_seconds
from webapp.identity import (
    InvalidIdentity,
    client_ip,
    is_allowlisted,
    limit_subject,
    user_from_headers,
)
from webapp.problems import Problem, openapi_example
from webapp.problems import install as install_problems
from webapp.settings import Backend, Settings
from webapp.simulate import Scenario, simulate

log = logging.getLogger(__name__)

EXPENSIVE_WEIGHT = 5


def too_many_requests(
    policy: str | None, retry_after: int | None, headers: dict[str, str]
) -> Problem:
    return Problem(
        429,
        "rate_limited",
        "Too many requests. Slow down and retry later.",
        headers=headers,
        policy=policy,
        retry_after=retry_after,
    )


def limiter_unavailable(retry_after: int) -> Problem:
    return Problem(
        503,
        "limiter_unavailable",
        "The rate limit store is unreachable. Try again shortly.",
        headers={"Retry-After": str(retry_after)},
        retry_after=retry_after,
    )


def _header(description: str) -> dict[str, Any]:
    return {"description": description, "schema": {"type": "integer"}}


_LIMIT_HEADERS = {
    "RateLimit-Limit": _header("Requests allowed by the limit you're closest to hitting"),
    "RateLimit-Remaining": _header("Requests left right now"),
    "RateLimit-Reset": _header("Seconds until the full limit is back"),
}
# Shown in /docs so API users can see the 429 and 503 shapes without triggering them.
LIMITED_RESPONSES: dict[int | str, dict[str, Any]] = {
    429: {
        "description": "Rate limited. Wait Retry-After seconds before trying again.",
        "headers": {**_LIMIT_HEADERS, "Retry-After": _header("Seconds to wait")},
        "content": openapi_example(too_many_requests("user", 1, {})),
    },
    503: {
        "description": "The limit store is down and FAIL_OPEN=false (no local fallback).",
        "headers": {"Retry-After": _header("Seconds to wait")},
        "content": openapi_example(limiter_unavailable(5)),
    },
}
DEMO_PAGE = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def _script_hashes(html: str) -> str:
    """CSP hashes of the page's inline scripts, so the policy can allow exactly those
    scripts and nothing an attacker manages to inject."""
    scripts = re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)
    digests = (base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() for s in scripts)
    return " ".join(f"'sha256-{d}'" for d in digests)


DEMO_CSP = "; ".join(
    [
        "default-src 'none'",
        f"script-src {_script_hashes(DEMO_PAGE)}",
        "style-src 'unsafe-inline'",  # one <style> block and two style="" attributes
        "connect-src 'self'",  # fetch() to /api/*
        "img-src 'self' data:",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def redis_client(settings: Settings) -> redis.Redis | None:
    if settings.backend is not Backend.REDIS:
        return None
    assert settings.redis_url  # checked in Settings
    return connect(
        settings.redis_url,
        timeout=settings.redis_timeout,
        max_connections=settings.redis_max_connections,
    )


def build_service(
    settings: Settings,
    client: redis.Redis | None = None,
    breaker: CircuitBreaker | None = None,
) -> RateLimitService:
    def local(algorithm: Algorithm, spec: LimitSpec) -> RateLimiter:
        return build_limiter(algorithm, spec)

    if settings.backend is Backend.REDIS:
        client = client or redis_client(settings)
        assert client is not None
        # One breaker for the one Redis: if it's down for the IP check, it's down for
        # the user check too.
        breaker = breaker or CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown)

        def make(algorithm: Algorithm, spec: LimitSpec) -> RateLimiter:
            return build_redis_limiter(
                algorithm, spec, client, prefix=settings.key_prefix, breaker=breaker
            )

        def fallback(algorithm: Algorithm, spec: LimitSpec) -> RateLimiter | None:
            return local(algorithm, spec) if settings.local_fallback else None

    else:
        make = local

        def fallback(algorithm: Algorithm, spec: LimitSpec) -> RateLimiter | None:
            return None  # memory can't be "down"

    algorithms = settings.algorithms
    batch: BatchHit | None = None
    # One script checks every key with the same algorithm, so batching needs the IP and
    # user limits to share one.
    one_algorithm = len(set(algorithms.values())) == 1
    if settings.backend is Backend.REDIS and settings.redis_batch and one_algorithm:
        batch = hit_many  # IP + user limit in one round trip, all or nothing

    def policy(name: str, scope: Scope, spec: LimitSpec) -> Policy:
        algorithm = algorithms[name]
        return Policy(
            name,
            scope,
            make(algorithm, spec),
            period=spec.period,
            fallback=fallback(algorithm, spec),
        )

    return RateLimitService(
        [
            policy("ip", Scope.IP, settings.ip_limit),
            policy("user", Scope.USER, settings.user_limit),
        ],
        fail_open=settings.fail_open,
        batch=batch,
    )


def warn_about_unreachable_weights(service: RateLimitService, weights: dict[str, int]) -> None:
    """A route that costs more than a limit's burst size can never succeed: it gets a
    429 with no Retry-After forever. That's almost always a config mistake (say
    USER_LIMIT=3/s with a route that costs 5), so say so at startup."""
    for route, weight in weights.items():
        for policy in service.policies:
            cost = policy.cost * weight
            if cost > policy.limiter.limit:
                log.warning(
                    "%s costs %d but the %r limit only allows bursts of %d, "
                    "so it will always get a 429",
                    route,
                    cost,
                    policy.name,
                    policy.limiter.limit,
                )


def create_app(
    settings: Settings | None = None, service: RateLimitService | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    client = None
    breaker = None
    if service is None:
        client = redis_client(settings)
        if client is not None:
            breaker = CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown)
        service = build_service(settings, client, breaker)
    if breaker is not None:
        b = breaker
        service.metrics.gauge("ratelimit_breaker_open", lambda: b.state is not BreakerState.CLOSED)
    # Keys held in process memory: the limiters themselves on the memory backend, the
    # fallbacks on Redis (0 until an outage). Lets you check the auto-sweep keeps it flat.
    in_memory = [
        limiter
        for policy in service.policies
        for limiter in (policy.limiter, policy.fallback)
        if isinstance(limiter, InMemoryLimiter)
    ]
    service.metrics.gauge("ratelimit_memory_keys", lambda: sum(len(lim) for lim in in_memory))

    warn_about_unreachable_weights(service, {"/api/report": EXPENSIVE_WEIGHT})

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if client is not None:
            client.close()  # give the pooled connections back on shutdown

    app = FastAPI(title="rate-limiter", version=__version__, lifespan=lifespan)
    install_problems(app)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.state.settings = settings
    app.state.service = service

    def identify(request: Request) -> Identity:
        peer = request.client.host if request.client else None
        ip = client_ip(
            peer, request.headers, settings.trusted_ip_header, settings.trusted_proxy_hops
        )
        request.state.allowlisted = is_allowlisted(ip, settings.ip_allowlist)
        request.state.client_ip = ip  # the real address; Identity.ip is what's limited
        try:
            user = user_from_headers(request.headers)
        except InvalidIdentity as exc:
            # Still count it against the IP. Otherwise a malformed header is a free pass:
            # every such request gets a 400 without ever touching a limit.
            if not request.state.allowlisted:
                reject_if_ip_limited(Identity(ip=limit_subject(ip)))
            raise Problem(400, "invalid_user_id", str(exc)) from exc
        return Identity(ip=limit_subject(ip), user=user)

    def reject_if_ip_limited(anonymous: Identity) -> None:
        try:
            verdict = service.check(anonymous)  # no user, so only IP policies apply
        except LimiterUnavailable:
            return  # it's getting a 400 anyway
        if not verdict.allowed:
            headers = rate_limit_headers(verdict, service.policies)
            raise too_many_requests("ip", int(headers.get("Retry-After", "0")) or None, headers)

    # A plain (sync) dependency: FastAPI runs it in a worker thread, so a slow
    # limiter store never blocks the event loop.
    def rate_limited(weight: int = 1) -> Callable[..., Verdict]:
        """A dependency that spends ``weight`` requests' worth of every limit."""

        def rate_limit(
            identity: Annotated[Identity, Depends(identify)],
            request: Request,
            response: Response,
        ) -> Verdict:
            if request.state.allowlisted:
                return Verdict(True, ())  # no limits, no headers
            started = time.perf_counter()
            try:
                verdict = service.check(identity, weight)
            except LimiterUnavailable as exc:  # only reached when FAIL_OPEN=false
                # While the breaker is open nothing will change until its cooldown
                # ends, so say exactly that instead of inviting a retry every second.
                wait = breaker.retry_after() if breaker is not None else 0.0
                raise limiter_unavailable(max(1, whole_seconds(wait))) from exc
            headers = rate_limit_headers(verdict, service.policies)
            # How long the limit check took, visible in browser dev tools. This is how the
            # Redis round trip was measured in production (see docs/LOAD_TEST.md).
            elapsed_ms = (time.perf_counter() - started) * 1000
            headers["Server-Timing"] = f"ratelimit;dur={elapsed_ms:.2f}"
            service.metrics.observe_latency(elapsed_ms)
            if not verdict.allowed:
                binding = verdict.binding
                raise too_many_requests(
                    binding.policy.name if binding else None,
                    int(headers.get("Retry-After", "0")) or None,
                    headers,
                )
            response.headers.update(headers)
            return verdict

        return rate_limit

    Limited = Annotated[Verdict, Depends(rate_limited())]
    Expensive = Annotated[Verdict, Depends(rate_limited(EXPENSIVE_WEIGHT))]

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def demo_page() -> HTMLResponse:
        return HTMLResponse(
            DEMO_PAGE,
            headers={"Cache-Control": "public, max-age=300", "Content-Security-Policy": DEMO_CSP},
        )

    @app.get("/api/simulate")
    def simulate_route(
        response: Response, scenario: Scenario = Scenario.EDGE_BURST
    ) -> dict[str, Any]:
        # Same input, same answer, until the code changes. Computing it is cheap; the
        # point is letting a CDN answer without running the app at all. s-maxage is
        # for shared caches (Vercel's edge, which clears its cache on every deploy).
        response.headers["Cache-Control"] = "public, max-age=3600, s-maxage=86400"
        return simulate(scenario)

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        """Liveness: the process is up. Doesn't touch Redis on purpose, so a Redis
        outage never gets healthy app servers restarted."""
        return {"ok": True}

    @app.get("/api/ready")
    def ready(response: Response) -> dict[str, Any]:
        """Readiness: can this server reach its limit store right now?"""
        if client is None:
            return {"ok": True, "backend": str(settings.backend)}
        started = time.perf_counter()
        try:
            client.ping()
        except (redis.ConnectionError, redis.TimeoutError) as exc:
            response.status_code = 503
            down: dict[str, Any] = {"ok": False, "backend": "redis", "error": type(exc).__name__}
            if breaker is not None:
                down["breaker"] = str(breaker.state)
            return down
        ping_ms = round((time.perf_counter() - started) * 1000, 2)
        body: dict[str, Any] = {"ok": True, "backend": "redis", "ping_ms": ping_ms}
        if breaker is not None:
            # Redis can answer this ping while the breaker is still open (it only tries
            # again every cooldown), so show both.
            body["breaker"] = str(breaker.state)
        return body

    @app.get("/api/metrics", response_class=PlainTextResponse)
    def metrics(request: Request) -> PlainTextResponse:
        """Prometheus text format. Per process: each server instance has its own."""
        if settings.metrics_token is not None:
            sent = request.headers.get("authorization", "")
            expected = f"Bearer {settings.metrics_token}"
            # compare_digest takes the same time however much of the token matches, so
            # response timing can't be used to guess it one character at a time.
            if not hmac.compare_digest(sent.encode(), expected.encode()):
                raise Problem(
                    401,
                    "unauthorized",
                    "metrics need a bearer token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return PlainTextResponse(service.metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {
            "version": __version__,
            "backend": str(settings.backend),
            "algorithm": str(settings.algorithm),
            "limits": {"ip": str(settings.ip_limit), "user": str(settings.user_limit)},
            "algorithms": {name: str(a) for name, a in settings.algorithms.items()},
            "redis_batch": settings.backend is Backend.REDIS
            and settings.redis_batch
            and len(set(settings.algorithms.values())) == 1,
            "local_fallback": settings.local_fallback,
            "fail_open": settings.fail_open,
        }

    @app.get("/api/hello", responses=LIMITED_RESPONSES)
    def hello(
        verdict: Limited, identity: Annotated[Identity, Depends(identify)], request: Request
    ) -> dict[str, Any]:
        return {
            "message": "hello",
            "ip": request.state.client_ip,
            "limited_as": identity.ip,  # same as ip, or the /64 for IPv6
            "user": identity.user,
        }

    @app.get("/api/report", responses=LIMITED_RESPONSES)
    def report(verdict: Expensive) -> dict[str, Any]:
        """Stands in for a slow, expensive endpoint: each call counts as 5 requests."""
        return {"message": "report ready", "cost": EXPENSIVE_WEIGHT}

    return app


app = create_app()
