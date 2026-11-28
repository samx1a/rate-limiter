"""Error responses as RFC 9457 "Problem Details" (application/problem+json).

A standard shape means generic HTTP clients, API gateways and docs tools understand
our errors without custom parsing:

    HTTP/1.1 429 Too Many Requests
    Content-Type: application/problem+json
    Retry-After: 2

    {"type": "/problems/rate-limited", "title": "Too Many Requests", "status": 429,
     "detail": "Too many requests. Slow down and retry later.",
     "error": "rate_limited", "policy": "user", "retry_after": 2}

``type`` says which kind of problem it is; ``error`` is the same thing as a plain code,
kept because it's easier to switch on. Extra fields carry the specifics.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MEDIA_TYPE = "application/problem+json"


class Problem(Exception):
    def __init__(
        self,
        status: int,
        error: str,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail
        self.headers = headers or {}
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {
            "type": "/problems/" + self.error.replace("_", "-"),
            "title": HTTPStatus(self.status).phrase,
            "status": self.status,
            "detail": self.detail,
            "error": self.error,
            **self.extra,
        }


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def problem_response(request: Request, problem: Problem) -> JSONResponse:
        return JSONResponse(
            problem.body(),
            status_code=problem.status,
            headers=problem.headers,
            media_type=MEDIA_TYPE,
        )


def openapi_example(problem: Problem) -> dict[str, Any]:
    """For ``responses=`` in route decorators, so /docs shows the real shape."""
    return {MEDIA_TYPE: {"example": problem.body()}}
