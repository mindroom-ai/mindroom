"""Validation helpers for server-side outbound HTTP(S) requests."""

from __future__ import annotations

import ipaddress
import socket
from typing import NoReturn
from urllib.parse import urljoin, urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_GENERIC_DENY_MESSAGE = "URL is not allowed for server-side fetching"
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    },
)
_METADATA_HOSTNAME_SUFFIXES = (
    ".metadata.google.internal",
    ".metadata.goog",
)
_METADATA_IP_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    },
)


class ServerFetchUrlError(ValueError):
    """Raised when a URL is not safe for a server-side outbound fetch."""

    def __init__(self, *, reason: str) -> None:
        super().__init__(_GENERIC_DENY_MESSAGE)
        self.reason = reason


def _deny(reason: str) -> NoReturn:
    raise ServerFetchUrlError(reason=reason)


def _normalize_hostname(hostname: str) -> str:
    """Normalize a URL hostname for validation checks."""
    host = hostname.rstrip(".").lower()
    if not host:
        _deny("missing_host")
    if "%" in host:
        _deny("invalid_host")
    return host


def _hostname_as_ascii(host: str) -> str:
    """Return an IDNA-normalized hostname for DNS lookups."""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise ServerFetchUrlError(reason="invalid_host") from e


def _ip_address_from_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return an IP address when the host is a direct address literal."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_local_hostname(host: str) -> bool:
    """Return whether a hostname is local-only by convention."""
    return host in _LOCAL_HOSTNAMES or host.endswith((".localhost", ".local"))


def _is_metadata_hostname(host: str) -> bool:
    """Return whether a hostname is a cloud metadata alias."""
    return host in _METADATA_HOSTNAMES or any(host.endswith(suffix) for suffix in _METADATA_HOSTNAME_SUFFIXES)


def _is_metadata_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an address is a known cloud/container metadata endpoint."""
    return address in _METADATA_IP_ADDRESSES


def _validate_ip_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private_networks: bool,
) -> None:
    """Reject addresses that are unsafe for the selected fetch policy."""
    if _is_metadata_ip(address):
        _deny("metadata_address")

    if address.is_multicast or address.is_reserved or address.is_unspecified:
        _deny("blocked_address")

    if allow_private_networks:
        return

    if not address.is_global:
        _deny("private_address")


def _resolve_host_addresses(
    host: str,
    *,
    port: int | None,
    scheme: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve a hostname to IP addresses for public-fetch validation."""
    service_port = port or (443 if scheme == "https" else 80)
    try:
        results = socket.getaddrinfo(
            host,
            service_port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as e:
        raise ServerFetchUrlError(reason="dns_resolution_failed") from e

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for result in results:
        sockaddr = result[4]
        if not sockaddr:
            continue
        raw_address = str(sockaddr[0]).split("%", maxsplit=1)[0]
        address = _ip_address_from_host(raw_address)
        if address is not None:
            addresses.append(address)

    if not addresses:
        _deny("dns_resolution_failed")
    return addresses


def validate_server_fetch_url(url: str, *, allow_private_networks: bool = False) -> str:
    """Validate that a URL is safe for a server-side HTTP(S) request."""
    normalized_url = url.strip()
    parsed = urlsplit(normalized_url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        _deny("unsupported_scheme")

    parsed_hostname = parsed.hostname
    if parsed_hostname is None:
        _deny("missing_host")

    host = _normalize_hostname(parsed_hostname)
    direct_address = _ip_address_from_host(host)
    if direct_address is not None:
        _validate_ip_address(direct_address, allow_private_networks=allow_private_networks)
        return normalized_url

    ascii_host = _hostname_as_ascii(host)
    if _is_metadata_hostname(ascii_host):
        _deny("metadata_hostname")

    if _is_local_hostname(ascii_host):
        if allow_private_networks:
            return normalized_url
        _deny("private_hostname")

    for address in _resolve_host_addresses(ascii_host, port=parsed.port, scheme=scheme):
        _validate_ip_address(address, allow_private_networks=allow_private_networks)

    return normalized_url


def validate_server_fetch_redirect_url(
    current_url: str,
    location: str | None,
    *,
    allow_private_networks: bool = False,
) -> str:
    """Resolve and validate an HTTP redirect Location value."""
    if not location:
        _deny("missing_redirect_location")
    return validate_server_fetch_url(urljoin(current_url, location), allow_private_networks=allow_private_networks)
