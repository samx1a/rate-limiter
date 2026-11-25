"""Configuration from environment variables (12-factor style)."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import TypeVar

from ratelimit.limits import Algorithm, LimitSpec, parse_limit

T = TypeVar("T")
E = TypeVar("E", bound=Enum)


class ConfigError(ValueError):
    """An environment variable has a value we can't use."""


def _flag(value: str) -> bool:
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected true or false")


class Backend(StrEnum):
    MEMORY = "memory"  # one server: thread-safe in-process state
    REDIS = "redis"  # many servers: shared state, atomic Lua scripts


DEFAULT_IP_LIMIT = LimitSpec(60, 60)  # 60 per minute per IP
DEFAULT_USER_LIMIT = LimitSpec(10, 10)  # 10 per 10 seconds per user


@dataclass(frozen=True, slots=True)
class Settings:
    backend: Backend = Backend.MEMORY
    redis_url: str | None = None
    key_prefix: str = "rl"
    # Check the IP and user limits in one Redis round trip, all or nothing. Turn off on
    # Redis Cluster, where the two keys usually live on different nodes.
    redis_batch: bool = True
    # Per server process. Hosted Redis plans cap total connections (free tier: 30).
    redis_max_connections: int = 10
    # Circuit breaker: stop calling Redis after this many failures in a row, then try
    # one request every breaker_cooldown seconds.
    breaker_threshold: int = 3
    breaker_cooldown: float = 5.0
    # Per-command Redis timeout. Keep it short in production (same region as Redis);
    # raise it when Redis is far away, e.g. running tests against a cloud database.
    redis_timeout: float = 0.25
    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    ip_limit: LimitSpec = DEFAULT_IP_LIMIT
    user_limit: LimitSpec = DEFAULT_USER_LIMIT
    trusted_ip_header: str | None = None
    # Clients that skip rate limiting entirely, e.g. your own monitoring or an
    # internal service. Matched against the resolved client IP.
    ip_allowlist: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    # While Redis is down: limit per server from memory (local_fallback), else let
    # everything through (fail_open) or refuse with 503.
    local_fallback: bool = True
    fail_open: bool = True

    def __post_init__(self) -> None:
        if self.backend is Backend.REDIS and not self.redis_url:
            raise ValueError("RATE_LIMIT_BACKEND=redis needs REDIS_URL")
        if self.backend is Backend.REDIS and self.algorithm is Algorithm.COUNTER:
            raise ValueError("the naive counter has no Redis version")
        if self.redis_timeout <= 0:
            raise ValueError("redis_timeout must be > 0 seconds")
        if self.breaker_threshold < 1 or self.breaker_cooldown <= 0:
            raise ValueError("breaker_threshold must be >= 1 and breaker_cooldown > 0")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read settings from environment variables.

        A bad value fails at startup with a message naming the variable, instead of a
        bare ``ValueError`` from deep inside a parser (or worse, a typo silently
        falling back to a default).
        """
        env = os.environ if env is None else env
        defaults = cls()

        def read(name: str, parse: Callable[[str], T], default: T) -> T:
            if name not in env:
                return default
            try:
                return parse(env[name])
            except ValueError as exc:
                raise ConfigError(f"{name}={env[name]!r}: {exc}") from None

        redis_url = env.get("REDIS_URL") or None
        # Default to Redis whenever a Redis URL is configured.
        backend_default = Backend.REDIS if redis_url else Backend.MEMORY
        try:
            return cls(
                backend=read("RATE_LIMIT_BACKEND", _choice(Backend), backend_default),
                redis_url=redis_url,
                key_prefix=read("RATE_LIMIT_PREFIX", str, defaults.key_prefix),
                redis_timeout=read("REDIS_TIMEOUT_MS", _millis, defaults.redis_timeout),
                redis_batch=read("REDIS_BATCH", _flag, defaults.redis_batch),
                redis_max_connections=read(
                    "REDIS_MAX_CONNECTIONS", _positive_int, defaults.redis_max_connections
                ),
                breaker_threshold=read(
                    "BREAKER_THRESHOLD", _positive_int, defaults.breaker_threshold
                ),
                breaker_cooldown=read("BREAKER_COOLDOWN_MS", _millis, defaults.breaker_cooldown),
                algorithm=read("RATE_LIMIT_ALGORITHM", _choice(Algorithm), defaults.algorithm),
                ip_limit=read("IP_LIMIT", parse_limit, defaults.ip_limit),
                user_limit=read("USER_LIMIT", parse_limit, defaults.user_limit),
                trusted_ip_header=env.get("TRUSTED_IP_HEADER") or None,
                ip_allowlist=read("IP_ALLOWLIST", _networks, defaults.ip_allowlist),
                local_fallback=read("LOCAL_FALLBACK", _flag, defaults.local_fallback),
                fail_open=read("FAIL_OPEN", _flag, defaults.fail_open),
            )
        except ConfigError:
            raise
        except ValueError as exc:  # cross-field checks in __post_init__
            raise ConfigError(str(exc)) from None


def _choice(enum: type[E]) -> Callable[[str], E]:
    def parse(value: str) -> E:
        try:
            return enum(value.strip().lower())
        except ValueError:
            options = ", ".join(str(m) for m in enum)
            raise ValueError(f"expected one of: {options}") from None

    return parse


def _networks(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Comma-separated IPs or CIDR ranges: ``10.0.0.0/8, 2001:db8::/32, 192.0.2.7``."""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    try:
        return tuple(ipaddress.ip_network(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"expected IPs or CIDR ranges ({exc})") from None


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ValueError("expected a whole number") from None
    if number < 1:
        raise ValueError("must be >= 1")
    return number


def _millis(value: str) -> float:
    try:
        ms = int(value)
    except ValueError:
        raise ValueError("expected a whole number of milliseconds") from None
    if ms <= 0:
        raise ValueError("must be > 0")
    return ms / 1000
