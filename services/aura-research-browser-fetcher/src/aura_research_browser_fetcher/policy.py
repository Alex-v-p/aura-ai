"""Browser destination validation; kept separate so the service remains standalone."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit


class DestinationBlocked(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedDestination:
    url: str
    hostname: str
    addresses: tuple[str, ...]


def _is_public(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        ip.is_global
        and not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    )


def _resolve(hostname: str) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DestinationBlocked("dns_failed") from exc
    addresses = tuple(sorted({str(info[4][0]) for info in infos}))
    if not addresses or not all(_is_public(address) for address in addresses):
        raise DestinationBlocked("non_public_address")
    return addresses


def validate_public_https_url(url: str) -> ValidatedDestination:
    parts: SplitResult = urlsplit(url)
    if parts.scheme.lower() != "https" or parts.port not in (None, 443):
        raise DestinationBlocked("scheme_or_port")
    if parts.username is not None or parts.password is not None:
        raise DestinationBlocked("userinfo")
    if parts.fragment:
        raise DestinationBlocked("fragment")
    if not parts.hostname:
        raise DestinationBlocked("missing_hostname")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise DestinationBlocked("invalid_hostname") from exc
    netloc = f"[{host}]" if ":" in host else host
    normalised = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    return ValidatedDestination(normalised, host, _resolve(host))


async def validate_public_https_url_async(url: str) -> ValidatedDestination:
    return await asyncio.to_thread(validate_public_https_url, url)
