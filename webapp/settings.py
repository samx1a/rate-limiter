"""Configuration from environment variables (12-factor style)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from ratelimit.limits import Algorithm, LimitSpec, parse_limit


def _flag(value: str) -> bool:
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected a true/false value, got {value!r}")


DEFAULT_IP_LIMIT = LimitSpec(60, 60)  # 60 per minute per IP
DEFAULT_USER_LIMIT = LimitSpec(10, 10)  # 10 per 10 seconds per user


@dataclass(frozen=True, slots=True)
class Settings:
    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    ip_limit: LimitSpec = DEFAULT_IP_LIMIT
    user_limit: LimitSpec = DEFAULT_USER_LIMIT
    trusted_ip_header: str | None = None
    fail_open: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        defaults = cls()
        return cls(
            algorithm=Algorithm(env.get("RATE_LIMIT_ALGORITHM", defaults.algorithm)),
            ip_limit=parse_limit(env["IP_LIMIT"]) if "IP_LIMIT" in env else defaults.ip_limit,
            user_limit=(
                parse_limit(env["USER_LIMIT"]) if "USER_LIMIT" in env else defaults.user_limit
            ),
            trusted_ip_header=env.get("TRUSTED_IP_HEADER") or None,
            fail_open=_flag(env["FAIL_OPEN"]) if "FAIL_OPEN" in env else defaults.fail_open,
        )
