from fastapi.testclient import TestClient

from ratelimit.limits import LimitSpec
from webapp.main import create_app
from webapp.settings import Settings


def client(ip_limit: int = 100, user_limit: int = 3) -> TestClient:
    settings = Settings(
        ip_limit=LimitSpec(ip_limit, 60),
        user_limit=LimitSpec(user_limit, 60),
        trusted_ip_header="x-real-ip",
    )
    return TestClient(create_app(settings))


def get(c: TestClient, user: str | None = "alice", ip: str = "203.0.113.1") -> int:
    headers = {"x-real-ip": ip}
    if user is not None:
        headers["x-user-id"] = user
    return c.get("/api/hello", headers=headers).status_code


def test_hello_returns_identity() -> None:
    c = client()
    body = c.get("/api/hello", headers={"x-real-ip": "203.0.113.1", "x-user-id": "alice"}).json()
    assert body == {"message": "hello", "ip": "203.0.113.1", "user": "alice"}


def test_user_limit_blocks_with_429() -> None:
    c = client(user_limit=3)
    assert [get(c) for _ in range(4)] == [200, 200, 200, 429]
    assert get(c, user="bob") == 200  # other users are unaffected


def test_ip_limit_applies_across_users() -> None:
    c = client(ip_limit=2, user_limit=10)
    assert [get(c, user=f"u{n}") for n in range(3)] == [200, 200, 429]
    assert get(c, user="u9", ip="203.0.113.2") == 200


def test_anonymous_requests_are_limited_by_ip() -> None:
    c = client(ip_limit=1)
    assert [get(c, user=None) for _ in range(2)] == [200, 429]


def test_bad_user_header_is_a_400() -> None:
    assert get(client(), user="not valid!") == 400


def test_health_and_info_are_not_limited() -> None:
    c = client(ip_limit=1)
    for _ in range(5):
        assert c.get("/api/health").status_code == 200
    info = c.get("/api/info").json()
    assert info["algorithm"] == "token_bucket"
    assert info["limits"] == {"ip": "1/60s", "user": "3/60s"}


def test_ready_without_redis() -> None:
    assert client().get("/api/ready").json() == {"ok": True, "backend": "memory"}


def test_ipv6_clients_share_a_limit_per_64() -> None:
    """Rotating through addresses in one /64 doesn't dodge the per-IP limit."""
    c = client(ip_limit=3)
    codes = [get(c, user=None, ip=f"2001:db8:1:2::{n:x}") for n in range(1, 5)]
    assert codes == [200, 200, 200, 429]
    assert get(c, user=None, ip="2001:db8:1:3::1") == 200  # a different /64


def test_expensive_endpoint_spends_five_at_once() -> None:
    c = client(user_limit=12)
    headers = {"x-real-ip": "203.0.113.1", "x-user-id": "alice"}
    first = c.get("/api/report", headers=headers)
    assert first.status_code == 200 and first.headers["RateLimit-Remaining"] == "7"
    assert c.get("/api/report", headers=headers).status_code == 200  # 2 left
    blocked = c.get("/api/report", headers=headers)
    assert blocked.status_code == 429  # 5 don't fit in 2...
    assert get(c) == 200  # ...but a normal request still does
