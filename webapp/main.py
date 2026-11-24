"""FastAPI wrapper around the rate limiting service.

No ``from __future__ import annotations`` here: FastAPI reads parameter annotations at
runtime, and string annotations cannot see aliases defined inside ``create_app``.
"""

import base64
import hashlib
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import redis
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from ratelimit.breaker import CircuitBreaker
from ratelimit.decision import LimiterUnavailable, RateLimiter
from ratelimit.limits import LimitSpec, build_limiter
from ratelimit.redis_limiters import build_redis_limiter, connect
from ratelimit.service import Identity, Policy, RateLimitService, Scope, Verdict
from webapp.headers import rate_limit_headers
from webapp.identity import (
    InvalidIdentity,
    client_ip,
    is_allowlisted,
    limit_subject,
    user_from_headers,
)
from webapp.settings import Backend, Settings
from webapp.simulate import Scenario, simulate

EXPENSIVE_WEIGHT = 5


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
        "content": {
            "application/json": {
                "example": {
                    "detail": {
                        "error": "rate_limited",
                        "message": "Too many requests. Slow down and retry later.",
                        "policy": "user",
                        "retry_after": 1,
                    }
                }
            }
        },
    },
    503: {
        "description": "The limit store is down and FAIL_OPEN=false (no local fallback).",
        "headers": {"Retry-After": _header("Seconds to wait")},
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
    return connect(settings.redis_url, timeout=settings.redis_timeout)


def build_service(settings: Settings, client: redis.Redis | None = None) -> RateLimitService:
    def local(spec: LimitSpec) -> RateLimiter:
        return build_limiter(settings.algorithm, spec)

    if settings.backend is Backend.REDIS:
        client = client or redis_client(settings)
        assert client is not None
        # One breaker for the one Redis: if it's down for the IP check, it's down for
        # the user check too.
        breaker = CircuitBreaker()

        def make(spec: LimitSpec) -> RateLimiter:
            return build_redis_limiter(
                settings.algorithm, spec, client, prefix=settings.key_prefix, breaker=breaker
            )

        def fallback(spec: LimitSpec) -> RateLimiter | None:
            return local(spec) if settings.local_fallback else None

    else:
        make = local

        def fallback(spec: LimitSpec) -> RateLimiter | None:
            return None  # memory can't be "down"

    def policy(name: str, scope: Scope, spec: LimitSpec) -> Policy:
        return Policy(name, scope, make(spec), period=spec.period, fallback=fallback(spec))

    return RateLimitService(
        [
            policy("ip", Scope.IP, settings.ip_limit),
            policy("user", Scope.USER, settings.user_limit),
        ],
        fail_open=settings.fail_open,
    )


def create_app(
    settings: Settings | None = None, service: RateLimitService | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    client = None
    if service is None:
        client = redis_client(settings)
        service = build_service(settings, client)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if client is not None:
            client.close()  # give the pooled connections back on shutdown

    app = FastAPI(title="rate-limiter", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.state.settings = settings
    app.state.service = service

    def identify(request: Request) -> Identity:
        try:
            user = user_from_headers(request.headers)
        except InvalidIdentity as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        peer = request.client.host if request.client else None
        ip = client_ip(peer, request.headers, settings.trusted_ip_header)
        request.state.allowlisted = is_allowlisted(ip, settings.ip_allowlist)
        return Identity(ip=limit_subject(ip), user=user)

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
                raise HTTPException(
                    status_code=503,
                    detail={"error": "limiter_unavailable", "message": "Try again shortly."},
                    headers={"Retry-After": "1"},
                ) from exc
            headers = rate_limit_headers(verdict, service.policies)
            # How long the limit check took, visible in browser dev tools. This is how the
            # Redis round trip was measured in production (see docs/LOAD_TEST.md).
            elapsed_ms = (time.perf_counter() - started) * 1000
            headers["Server-Timing"] = f"ratelimit;dur={elapsed_ms:.2f}"
            if not verdict.allowed:
                binding = verdict.binding
                raise HTTPException(
                    status_code=429,
                    detail={
                        "error": "rate_limited",
                        "message": "Too many requests. Slow down and retry later.",
                        "policy": binding.policy.name if binding else None,
                        "retry_after": int(headers.get("Retry-After", "0")) or None,
                    },
                    headers=headers,
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
    def simulate_route(scenario: Scenario = Scenario.EDGE_BURST) -> dict[str, Any]:
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
            return {"ok": False, "backend": "redis", "error": type(exc).__name__}
        ping_ms = round((time.perf_counter() - started) * 1000, 2)
        return {"ok": True, "backend": "redis", "ping_ms": ping_ms}

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {
            "backend": str(settings.backend),
            "algorithm": str(settings.algorithm),
            "limits": {"ip": str(settings.ip_limit), "user": str(settings.user_limit)},
            "local_fallback": settings.local_fallback,
            "fail_open": settings.fail_open,
        }

    @app.get("/api/hello", responses=LIMITED_RESPONSES)
    def hello(verdict: Limited, identity: Annotated[Identity, Depends(identify)]) -> dict[str, Any]:
        return {"message": "hello", "ip": identity.ip, "user": identity.user}

    @app.get("/api/report", responses=LIMITED_RESPONSES)
    def report(verdict: Expensive) -> dict[str, Any]:
        """Stands in for a slow, expensive endpoint: each call counts as 5 requests."""
        return {"message": "report ready", "cost": EXPENSIVE_WEIGHT}

    return app


app = create_app()
