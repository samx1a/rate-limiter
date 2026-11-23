"""FastAPI wrapper around the rate limiting service.

No ``from __future__ import annotations`` here: FastAPI reads parameter annotations at
runtime, and string annotations cannot see aliases defined inside ``create_app``.
"""

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request

from ratelimit.limits import build_limiter
from ratelimit.service import Identity, Policy, RateLimitService, Scope, Verdict
from webapp.identity import InvalidIdentity, client_ip, user_from_headers
from webapp.settings import Settings


def build_service(settings: Settings) -> RateLimitService:
    return RateLimitService(
        [
            Policy(
                "ip",
                Scope.IP,
                build_limiter(settings.algorithm, settings.ip_limit),
                period=settings.ip_limit.period,
            ),
            Policy(
                "user",
                Scope.USER,
                build_limiter(settings.algorithm, settings.user_limit),
                period=settings.user_limit.period,
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
    def rate_limit(identity: Annotated[Identity, Depends(identify)]) -> Verdict:
        verdict = service.check(identity)
        if not verdict.allowed:
            raise HTTPException(status_code=429, detail="Too many requests")
        return verdict

    Limited = Annotated[Verdict, Depends(rate_limit)]

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/info")
    def info() -> dict[str, Any]:
        return {
            "algorithm": str(settings.algorithm),
            "limits": {"ip": str(settings.ip_limit), "user": str(settings.user_limit)},
            "fail_open": settings.fail_open,
        }

    @app.get("/api/hello")
    def hello(verdict: Limited, identity: Annotated[Identity, Depends(identify)]) -> dict[str, Any]:
        return {"message": "hello", "ip": identity.ip, "user": identity.user}

    return app


app = create_app()
