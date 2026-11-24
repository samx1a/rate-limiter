import pytest

from webapp.identity import InvalidIdentity, client_ip, limit_subject, user_from_headers


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, None),
        ({"x-user-id": ""}, None),
        ({"x-user-id": "   "}, None),
        ({"x-user-id": "alice"}, "alice"),
        ({"x-user-id": "  bob.smith@example.com "}, "bob.smith@example.com"),
        ({"x-user-id": "a" * 64}, "a" * 64),
    ],
)
def test_user_from_headers(headers: dict[str, str], expected: str | None) -> None:
    assert user_from_headers(headers) == expected


@pytest.mark.parametrize("bad", ["a" * 65, "alice bob", "x:y", "../etc", "名字", "a\nb"])
def test_user_rejects_malformed_ids(bad: str) -> None:
    with pytest.raises(InvalidIdentity):
        user_from_headers({"x-user-id": bad})


def test_ip_uses_the_peer_by_default_and_ignores_spoofable_headers() -> None:
    headers = {"x-forwarded-for": "6.6.6.6", "x-real-ip": "6.6.6.6"}
    assert client_ip("10.0.0.7", headers, trusted_ip_header=None) == "10.0.0.7"


def test_ip_uses_the_trusted_header_when_configured() -> None:
    assert client_ip("10.0.0.7", {"x-real-ip": "203.0.113.9"}, "X-Real-IP") == "203.0.113.9"


def test_ip_takes_the_first_hop_of_a_list() -> None:
    headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
    assert client_ip("10.0.0.7", headers, "x-forwarded-for") == "203.0.113.9"


def test_ip_falls_back_to_peer_when_trusted_header_is_missing_or_garbage() -> None:
    assert client_ip("10.0.0.7", {}, "x-real-ip") == "10.0.0.7"
    assert client_ip("10.0.0.7", {"x-real-ip": "not-an-ip"}, "x-real-ip") == "10.0.0.7"


def test_ipv6_and_unknown_peer() -> None:
    assert client_ip("::1", {}, None) == "::1"
    assert client_ip(None, {}, None) == "unknown"
    assert client_ip("testclient", {}, None) == "unknown"


@pytest.mark.parametrize(
    ("ip", "subject"),
    [
        ("203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),  # the next /64 is someone else
        ("::ffff:198.51.100.4", "198.51.100.4"),
        ("unknown", "unknown"),
    ],
)
def test_limit_subject(ip: str, subject: str) -> None:
    assert limit_subject(ip) == subject
