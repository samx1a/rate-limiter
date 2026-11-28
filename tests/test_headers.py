import math

from fastapi.testclient import TestClient

from ratelimit import Decision
from ratelimit.algorithms.token_bucket import TokenBucket
from ratelimit.limits import LimitSpec
from ratelimit.service import Policy, PolicyResult, Scope, Verdict
from webapp.headers import rate_limit_headers, whole_seconds
from webapp.main import create_app
from webapp.settings import Settings


def client(ip: int = 100, user: int = 3) -> TestClient:
    settings = Settings(
        ip_limit=LimitSpec(ip, 60), user_limit=LimitSpec(user, 30), trusted_ip_header="x-real-ip"
    )
    return TestClient(create_app(settings))


HEADERS = {"x-real-ip": "203.0.113.1", "x-user-id": "alice"}


def test_allowed_responses_carry_limit_headers() -> None:
    c = client(user=3)
    first = c.get("/api/hello", headers=HEADERS)
    assert first.status_code == 200
    assert first.headers["RateLimit-Limit"] == "3"  # the user policy binds (2 left vs 99)
    assert first.headers["RateLimit-Remaining"] == "2"
    assert first.headers["RateLimit-Reset"] == "10"  # 1 token at 3 per 30s = 10s
    assert first.headers["RateLimit-Policy"] == '100;w=60;name="ip", 3;w=30;name="user"'
    assert "Retry-After" not in first.headers
    assert first.headers["Server-Timing"].startswith("ratelimit;dur=")
    second = c.get("/api/hello", headers=HEADERS)
    assert second.headers["RateLimit-Remaining"] == "1"


def test_blocked_response_is_429_with_retry_after_and_json_body() -> None:
    c = client(user=2)
    for _ in range(2):
        c.get("/api/hello", headers=HEADERS)
    blocked = c.get("/api/hello", headers=HEADERS)
    assert blocked.status_code == 429
    assert blocked.headers["RateLimit-Remaining"] == "0"
    assert blocked.headers["Retry-After"] == "15"  # 1 token at 2 per 30s
    assert blocked.headers["RateLimit-Reset"] == "30"
    assert blocked.headers["Content-Type"] == "application/problem+json"
    assert blocked.json() == {
        "type": "/problems/rate-limited",
        "title": "Too Many Requests",
        "status": 429,
        "detail": "Too many requests. Slow down and retry later.",
        "error": "rate_limited",
        "policy": "user",
        "retry_after": 15,
    }


def test_ip_block_reports_the_ip_policy() -> None:
    c = client(ip=1, user=10)
    c.get("/api/hello", headers=HEADERS)
    blocked = c.get("/api/hello", headers=HEADERS)
    assert blocked.status_code == 429
    assert blocked.headers["RateLimit-Limit"] == "1"
    assert blocked.json()["policy"] == "ip"


def test_unlimited_routes_have_no_headers() -> None:
    assert "RateLimit-Limit" not in client().get("/api/health").headers


def _verdict(decision: Decision) -> tuple[Verdict, list[Policy]]:
    policy = Policy("p", Scope.IP, TokenBucket(5, 1.0), period=5)
    return Verdict(decision.allowed, (PolicyResult(policy, decision),)), [policy]


def test_retry_after_is_at_least_one_and_rounded_up() -> None:
    verdict, policies = _verdict(Decision(False, 5, 0, 0.2, retry_after=0.01))
    assert rate_limit_headers(verdict, policies)["Retry-After"] == "1"
    verdict, policies = _verdict(Decision(False, 5, 0, 2.5, retry_after=2.5))
    assert rate_limit_headers(verdict, policies)["Retry-After"] == "3"


def test_infinite_values_are_left_out() -> None:
    verdict, policies = _verdict(Decision(False, 5, 0, math.inf, retry_after=math.inf))
    headers = rate_limit_headers(verdict, policies)
    assert "Retry-After" not in headers and "RateLimit-Reset" not in headers


def test_whole_seconds_ignores_float_noise() -> None:
    assert whole_seconds(2.0000000000001) == 2
    assert whole_seconds(2.01) == 3
    assert whole_seconds(0) == 0
    assert rate_limit_headers(Verdict(True, ()), []) == {}
