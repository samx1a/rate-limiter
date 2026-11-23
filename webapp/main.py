"""FastAPI wrapper around the rate limiting service.

No ``from __future__ import annotations`` here: FastAPI reads parameter annotations at
runtime, and string annotations cannot see aliases defined inside ``create_app``.
"""

from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

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
    if settings.backend is Backend.REDIS:
        assert settings.redis_url  # checked in Settings
        client = connect(settings.redis_url)

        def make(spec: LimitSpec) -> RateLimiter:
            return build_redis_limiter(settings.algorithm, spec, client, prefix=settings.key_prefix)

    else:

        def make(spec: LimitSpec) -> RateLimiter:
            return build_limiter(settings.algorithm, spec)

    return RateLimitService(
        [
            Policy("ip", Scope.IP, make(settings.ip_limit), period=settings.ip_limit.period),
            Policy(
                "user", Scope.USER, make(settings.user_limit), period=settings.user_limit.period
            ),
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
        try:
            verdict = service.check(identity)
        except LimiterUnavailable as exc:  # only reached when FAIL_OPEN=false
            raise HTTPException(
                status_code=503,
                detail={"error": "limiter_unavailable", "message": "Try again shortly."},
                headers={"Retry-After": "1"},
            ) from exc
        headers = rate_limit_headers(verdict, service.policies)
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
            "fail_open": settings.fail_open,
        }

    @app.get("/api/hello")
    def hello(verdict: Limited, identity: Annotated[Identity, Depends(identify)]) -> dict[str, Any]:
        return {"message": "hello", "ip": identity.ip, "user": identity.user}

    return app


app = create_app()
