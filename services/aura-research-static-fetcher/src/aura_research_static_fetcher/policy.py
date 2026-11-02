"""Public HTTPS destination policy used at every request and redirect hop."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit


class DestinationBlocked(ValueError):
    """Raised when a URL cannot be proven to target a public HTTPS endpoint."""


@dataclass(frozen=True)
class ValidatedDestination:
    url: str
    hostname: str
    addresses: tuple[str, ...]


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    # is_global alone permits a few special ranges on older Python versions;
    # explicitly reject every non-public category as a defence in depth check.
    return bool(
        address.is_global
        and not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
    )


def _normalise(parts: SplitResult) -> str:
    hostname = parts.hostname
    if hostname is None:
        raise DestinationBlocked("missing_hostname")
    try:
        host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise DestinationBlocked("invalid_hostname") from exc
    if parts.username is not None or parts.password is not None:
        raise DestinationBlocked("userinfo")
    if parts.scheme.lower() != "https":
        raise DestinationBlocked("scheme")
    if parts.port not in (None, 443):
        raise DestinationBlocked("port")
    if parts.fragment:
        raise DestinationBlocked("fragment")
    # urlsplit.hostname removes brackets from IPv6 literals.
    netloc = f"[{host}]" if ":" in host else host
    return urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))


def _resolve(hostname: str) -> tuple[str, ...]:
    try:
        # AF_UNSPEC is intentional: a mixed A/AAAA answer must be rejected,
        # rather than silently preferring whichever family happens to work.
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DestinationBlocked("dns_failed") from exc
    addresses = tuple(sorted({str(info[4][0]) for info in infos}))
    if not addresses or not all(_public_address(address) for address in addresses):
        raise DestinationBlocked("non_public_address")
    return addresses


def validate_public_https_url(url: str) -> ValidatedDestination:
    """Canonicalise and resolve a URL immediately before opening a connection."""

    parts = urlsplit(url)
    normalised = _normalise(parts)
    hostname = urlsplit(normalised).hostname
    assert hostname is not None
    addresses = _resolve(hostname)
    return ValidatedDestination(normalised, hostname, addresses)


async def validate_public_https_url_async(url: str) -> ValidatedDestination:
    """Async wrapper so DNS work cannot block the provider event loop."""

    return await asyncio.to_thread(validate_public_https_url, url)
