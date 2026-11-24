"""FastAPI wrapper around the rate limiting service.

No ``from __future__ import annotations`` here: FastAPI reads parameter annotations at
runtime, and string annotations cannot see aliases defined inside ``create_app``.
"""

import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from ratelimit.breaker import CircuitBreaker
from ratelimit.decision import LimiterUnavailable, RateLimiter
from ratelimit.limits import LimitSpec, build_limiter
from ratelimit.redis_limiters import build_redis_limiter, connect
from ratelimit.service import Identity, Policy, RateLimitService, Scope, Verdict
from webapp.headers import rate_limit_headers
from webapp.identity import InvalidIdentity, client_ip, user_from_headers
from webapp.settings import Backend, Settings
from webapp.simulate import Scenario, simulate

DEMO_PAGE = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def build_service(settings: Settings) -> RateLimitService:
    def local(spec: LimitSpec) -> RateLimiter:
        return build_limiter(settings.algorithm, spec)

    if settings.backend is Backend.REDIS:
        assert settings.redis_url  # checked in Settings
        client = connect(settings.redis_url, timeout=settings.redis_timeout)
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
    service = service or build_service(settings)
    app = FastAPI(title="rate-limiter", version="0.1.0")
    app.state.settings = settings
    app.state.service = service

    def identify(request: Request) -> Identity:
        try:
            user = user_from_headers(request.headers)
        except InvalidIdentity as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        peer = request.client.host if request.client else None
        return Identity(ip=client_ip(peer, request.headers, settings.trusted_ip_header), user=user)

    # A plain (sync) dependency: FastAPI runs it in a worker thread, so a slow
    # limiter store never blocks the event loop.
    def rate_limit(identity: Annotated[Identity, Depends(identify)], response: Response) -> Verdict:
        started = time.perf_counter()
        try:
            verdict = service.check(identity)
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

    Limited = Annotated[Verdict, Depends(rate_limit)]

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def demo_page() -> HTMLResponse:
        return HTMLResponse(DEMO_PAGE, headers={"Cache-Control": "public, max-age=300"})

    @app.get("/api/simulate")
    def simulate_route(scenario: Scenario = Scenario.EDGE_BURST) -> dict[str, Any]:
        return simulate(scenario)

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {
            "backend": str(settings.backend),
            "algorithm": str(settings.algorithm),
            "limits": {"ip": str(settings.ip_limit), "user": str(settings.user_limit)},
            "local_fallback": settings.local_fallback,
            "fail_open": settings.fail_open,
        }

    @app.get("/api/hello")
    def hello(verdict: Limited, identity: Annotated[Identity, Depends(identify)]) -> dict[str, Any]:
        return {"message": "hello", "ip": identity.ip, "user": identity.user}

    return app


app = create_app()
