"""Diagnostic-only public HTTPS probe; never a production readiness attestation."""

from __future__ import annotations

import argparse
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit


def _public(address: str) -> bool:
    value = ipaddress.ip_address(address)
    return value.is_global and not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_reserved
        or value.is_multicast
        or value.is_unspecified
    )


def probe(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.port not in (None, 443) or not parsed.hostname:
        raise SystemExit("egress_probe_failed")
    addresses = sorted(
        {
            str(info[4][0])
            for info in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    )
    if not addresses or not all(_public(address) for address in addresses):
        raise SystemExit("egress_probe_failed")
    context = ssl.create_default_context()
    with socket.create_connection((addresses[0], 443), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=parsed.hostname) as tls:
            if tls.version() is None:
                raise SystemExit("egress_probe_failed")
    print("egress_probe_ok")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--url", required=True)
    arguments = parser.parse_args()
    probe(arguments.url)
