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
