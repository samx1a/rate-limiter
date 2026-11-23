"""Configuration from environment variables (12-factor style)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ratelimit.limits import Algorithm, LimitSpec, parse_limit


def _flag(value: str) -> bool:
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected a true/false value, got {value!r}")


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
    # Per-command Redis timeout. Keep it short in production (same region as Redis);
    # raise it when Redis is far away, e.g. running tests against a cloud database.
    redis_timeout: float = 0.25
    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    ip_limit: LimitSpec = DEFAULT_IP_LIMIT
    user_limit: LimitSpec = DEFAULT_USER_LIMIT
    trusted_ip_header: str | None = None
    fail_open: bool = True

    def __post_init__(self) -> None:
        if self.backend is Backend.REDIS and not self.redis_url:
            raise ValueError("RATE_LIMIT_BACKEND=redis needs REDIS_URL")
        if self.backend is Backend.REDIS and self.algorithm is Algorithm.COUNTER:
            raise ValueError("the naive counter has no Redis version")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        defaults = cls()
        redis_url = env.get("REDIS_URL") or None
        # Default to Redis whenever a Redis URL is configured.
        backend = env.get("RATE_LIMIT_BACKEND") or ("redis" if redis_url else "memory")
        return cls(
            backend=Backend(backend),
            redis_url=redis_url,
            key_prefix=env.get("RATE_LIMIT_PREFIX", defaults.key_prefix),
            redis_timeout=(
                int(env["REDIS_TIMEOUT_MS"]) / 1000
                if "REDIS_TIMEOUT_MS" in env
                else defaults.redis_timeout
            ),
            algorithm=Algorithm(env.get("RATE_LIMIT_ALGORITHM", defaults.algorithm)),
            ip_limit=parse_limit(env["IP_LIMIT"]) if "IP_LIMIT" in env else defaults.ip_limit,
            user_limit=(
                parse_limit(env["USER_LIMIT"]) if "USER_LIMIT" in env else defaults.user_limit
            ),
            trusted_ip_header=env.get("TRUSTED_IP_HEADER") or None,
            fail_open=_flag(env["FAIL_OPEN"]) if "FAIL_OPEN" in env else defaults.fail_open,
        )
