import ipaddress

import pytest

from ratelimit.limits import Algorithm, LimitSpec
from webapp.settings import Backend, ConfigError, Settings


def test_every_variable_is_read() -> None:
    s = Settings.from_env(
        {
            "RATE_LIMIT_BACKEND": "Redis",
            "REDIS_URL": "redis://x:1/0",
            "RATE_LIMIT_PREFIX": "app",
            "REDIS_TIMEOUT_MS": "500",
            "REDIS_BATCH": "off",
            "REDIS_MAX_CONNECTIONS": "4",
            "BREAKER_THRESHOLD": "5",
            "BREAKER_COOLDOWN_MS": "2000",
            "RATE_LIMIT_ALGORITHM": "fixed_window",
            "IP_LIMIT": "100/1m",
            "USER_LIMIT": "5/s",
            "TRUSTED_IP_HEADER": "x-real-ip",
            "TRUSTED_PROXY_HOPS": "2",
            "LOCAL_FALLBACK": "false",
            "FAIL_OPEN": "no",
        }
    )
    assert s == Settings(
        backend=Backend.REDIS,
        redis_url="redis://x:1/0",
        key_prefix="app",
        redis_timeout=0.5,
        redis_batch=False,
        redis_max_connections=4,
        breaker_threshold=5,
        breaker_cooldown=2.0,
        algorithm=Algorithm.FIXED_WINDOW,
        ip_limit=LimitSpec(100, 60),
        user_limit=LimitSpec(5, 1),
        trusted_ip_header="x-real-ip",
        trusted_proxy_hops=2,
        local_fallback=False,
        fail_open=False,
    )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("REDIS_TIMEOUT_MS", "0.5", "whole number of milliseconds"),
        ("REDIS_TIMEOUT_MS", "0", "must be > 0"),
        ("RATE_LIMIT_ALGORITHM", "leaky", "expected one of: counter, fixed_window"),
        ("RATE_LIMIT_BACKEND", "memcached", "expected one of: memory, redis"),
        ("IP_LIMIT", "lots", "bad limit"),
        ("USER_LIMIT", "0/s", "at least 1 request"),
        ("FAIL_OPEN", "maybe", "expected true or false"),
        ("BREAKER_THRESHOLD", "0", "must be >= 1"),
        ("BREAKER_THRESHOLD", "three", "expected a whole number"),
        ("BREAKER_COOLDOWN_MS", "-5", "must be > 0"),
    ],
)
def test_bad_values_name_the_variable(name: str, value: str, message: str) -> None:
    with pytest.raises(ConfigError) as info:
        Settings.from_env({name: value})
    assert str(info.value).startswith(f"{name}={value!r}: ")
    assert message in str(info.value)


def test_cross_field_problems_are_config_errors_too() -> None:
    with pytest.raises(ConfigError, match="needs REDIS_URL"):
        Settings.from_env({"RATE_LIMIT_BACKEND": "redis"})


def test_defaults_and_partial_env() -> None:
    s = Settings.from_env(
        {
            "RATE_LIMIT_ALGORITHM": "sliding_window",
            "IP_LIMIT": "100/1m",
            "USER_LIMIT": "5/10s",
            "TRUSTED_IP_HEADER": "x-real-ip",
            "FAIL_OPEN": "false",
        }
    )
    assert str(s.algorithm) == "sliding_window"
    assert s.ip_limit == LimitSpec(100, 60) and s.user_limit == LimitSpec(5, 10)
    assert s.trusted_ip_header == "x-real-ip" and s.fail_open is False
    assert Settings.from_env({}) == Settings()


def test_settings_pick_redis_when_a_url_is_set() -> None:
    s = Settings.from_env({"REDIS_URL": "redis://example:6379/0"})
    assert str(s.backend) == "redis"
    assert str(Settings.from_env({}).backend) == "memory"


def test_redis_backend_needs_a_url_and_a_redis_algorithm() -> None:
    with pytest.raises(ValueError):
        Settings(backend=Backend.REDIS)
    with pytest.raises(ValueError):
        Settings.from_env({"REDIS_URL": "redis://x:1/0", "RATE_LIMIT_ALGORITHM": "counter"})


def test_ip_allowlist() -> None:
    s = Settings.from_env({"IP_ALLOWLIST": "10.0.0.0/8, 192.0.2.7 ,2001:db8::/32,"})
    assert s.ip_allowlist == (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("192.0.2.7/32"),
        ipaddress.ip_network("2001:db8::/32"),
    )
    with pytest.raises(ConfigError, match=r"IP_ALLOWLIST='10\.0\.0\.1/8': expected IPs or CIDR"):
        Settings.from_env({"IP_ALLOWLIST": "10.0.0.1/8"})  # host bits set: likely a typo
