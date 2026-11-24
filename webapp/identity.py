"""Work out who is making a request: their IP address and (optionally) user id.

Trust boundaries matter here. A client can send any header it likes, so:

- **IP**: by default we use the TCP peer address. Behind a proxy or load balancer
  that is the proxy's address, so the real client IP comes from a header the proxy
  *sets* (and overwrites if the client sent one). Name that header in
  ``trusted_ip_header`` only if your proxy really does that. On Vercel it is
  ``x-real-ip``. Trusting ``x-forwarded-for`` without such a proxy would let anyone
  pick their own IP and dodge per-IP limits.
  If no valid IP can be found, the request is counted under ``"unknown"``, so all
  such requests share one (strict) bucket rather than escaping the limit.
- **IPv6**: one home connection or one cloud VM usually gets a whole /64, which is
  2^64 addresses. Limiting each address separately would let anyone dodge the per-IP
  limit by picking a new address per request, so IPv6 clients are limited per /64.
  IPv4-mapped addresses (``::ffff:1.2.3.4``) count as the IPv4 address they wrap.
- **User**: this demo reads ``X-User-Id`` and validates its shape. In a real system
  the user id comes from a verified session or token, never a raw header.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping

USER_HEADER = "x-user-id"
_USER_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


class InvalidIdentity(ValueError):
    """The request carried a malformed identity header."""


def user_from_headers(headers: Mapping[str, str]) -> str | None:
    raw = headers.get(USER_HEADER)
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if not _USER_ID.fullmatch(value):
        raise InvalidIdentity(
            "X-User-Id must be 1-64 characters: letters, digits, '_', '.', '@' or '-'"
        )
    return value


def client_ip(peer: str | None, headers: Mapping[str, str], trusted_ip_header: str | None) -> str:
    if trusted_ip_header:
        raw = headers.get(trusted_ip_header.lower())
        if raw:
            # A proxy that sets the header may still list several hops; the first one
            # is the client it saw.
            candidate = raw.split(",")[0].strip()
            if _is_ip(candidate):
                return candidate
    if peer and _is_ip(peer):
        return peer
    return "unknown"


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


IPV6_PREFIX = 64


def limit_subject(ip: str, ipv6_prefix: int = IPV6_PREFIX) -> str:
    """The string the per-IP limit is keyed on: the IPv4 address itself, or the IPv6
    network it belongs to (e.g. ``2001:db8:1:2::/64``)."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip  # "unknown"
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, ipv6_prefix), strict=False))
    return str(address)


def is_allowlisted(
    ip: str, allowlist: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
) -> bool:
    if not allowlist:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False  # "unknown" is never trusted
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in allowlist)
