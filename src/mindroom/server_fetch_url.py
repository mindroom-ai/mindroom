"""Validation helpers for server-side outbound HTTP(S) requests."""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from typing import NoReturn
    from urllib.parse import SplitResult

    from httpcore._backends.base import SOCKET_OPTION

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
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


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


def _ip_address_from_host(host: str) -> _IPAddress | None:
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


def _is_metadata_ip(address: _IPAddress) -> bool:
    """Return whether an address is a known cloud/container metadata endpoint."""
    return address in _METADATA_IP_ADDRESSES


def _embedded_ipv4_addresses(address: _IPAddress) -> tuple[ipaddress.IPv4Address, ...]:
    """Return IPv4 addresses embedded in IPv6 transition forms."""
    if not isinstance(address, ipaddress.IPv6Address):
        return ()

    addresses: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        addresses.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        addresses.append(address.sixtofour)
    if address.teredo is not None:
        server_address, client_address = address.teredo
        addresses.extend((server_address, client_address))
    return tuple(addresses)


def _validate_ip_address(
    address: _IPAddress,
    *,
    allow_private_networks: bool,
) -> None:
    """Reject addresses that are unsafe for the selected fetch policy."""
    checked_addresses = (address, *_embedded_ipv4_addresses(address))
    for checked_address in checked_addresses:
        if _is_metadata_ip(checked_address):
            _deny("metadata_address")

        if (
            checked_address.is_link_local
            or checked_address.is_multicast
            or checked_address.is_reserved
            or checked_address.is_unspecified
        ):
            _deny("blocked_address")

    if allow_private_networks:
        return

    if not address.is_global:
        _deny("private_address")


def _resolve_host_addresses(host: str, *, port: int | None, scheme: str) -> list[_IPAddress]:
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

    addresses: list[_IPAddress] = []
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


def _parse_port(parsed_url: SplitResult) -> int | None:
    """Return a parsed URL port or raise the shared validation error."""
    try:
        return parsed_url.port
    except ValueError as e:
        raise ServerFetchUrlError(reason="invalid_port") from e


def _parse_hostname(parsed_url: SplitResult) -> str | None:
    """Return a parsed URL hostname or raise the shared validation error."""
    try:
        return parsed_url.hostname
    except ValueError as e:
        raise ServerFetchUrlError(reason="invalid_host") from e


def _validated_host_addresses(
    hostname: str,
    *,
    port: int | None,
    scheme: str,
    allow_private_networks: bool,
    resolve_hostnames: bool,
) -> list[_IPAddress]:
    """Validate a host and optionally return addresses that are safe to dial."""
    host = _normalize_hostname(hostname)
    direct_address = _ip_address_from_host(host)
    if direct_address is not None:
        _validate_ip_address(direct_address, allow_private_networks=allow_private_networks)
        return [direct_address]

    ascii_host = _hostname_as_ascii(host)
    if _is_metadata_hostname(ascii_host):
        _deny("metadata_hostname")

    if _is_local_hostname(ascii_host) and not allow_private_networks:
        _deny("private_hostname")
    if not resolve_hostnames:
        return []

    addresses = _resolve_host_addresses(ascii_host, port=port, scheme=scheme)
    for address in addresses:
        _validate_ip_address(address, allow_private_networks=allow_private_networks)
    return addresses


def _validate_server_fetch_url(url: str, *, allow_private_networks: bool, resolve_hostnames: bool) -> str:
    """Validate a server-fetch URL, optionally resolving hostnames immediately."""
    normalized_url = url.strip()
    try:
        parsed = urlsplit(normalized_url)
    except ValueError as e:
        raise ServerFetchUrlError(reason="invalid_host") from e

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        _deny("unsupported_scheme")

    parsed_hostname = _parse_hostname(parsed)
    if parsed_hostname is None:
        _deny("missing_host")

    _validated_host_addresses(
        parsed_hostname,
        port=_parse_port(parsed),
        scheme=scheme,
        allow_private_networks=allow_private_networks,
        resolve_hostnames=resolve_hostnames,
    )
    return normalized_url


def validate_server_fetch_url(url: str, *, allow_private_networks: bool = False) -> str:
    """Validate that a URL is safe for a server-side HTTP(S) request."""
    return _validate_server_fetch_url(
        url,
        allow_private_networks=allow_private_networks,
        resolve_hostnames=True,
    )


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


def _validated_connect_addresses(
    host: str,
    *,
    port: int,
    allow_private_networks: bool,
) -> list[_IPAddress]:
    """Resolve and validate the addresses used for an actual TCP connection."""
    return _validated_host_addresses(
        host,
        port=port,
        scheme="http",
        allow_private_networks=allow_private_networks,
        resolve_hostnames=True,
    )


class _ServerFetchSyncNetworkBackend(httpcore.NetworkBackend):
    """httpcore network backend that validates the address it dials."""

    def __init__(self, *, allow_private_networks: bool) -> None:
        self._allow_private_networks = allow_private_networks
        self._backend: httpcore.NetworkBackend = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        return _connect_validated_sync(
            _validated_connect_addresses(host, port=port, allow_private_networks=self._allow_private_networks),
            lambda address: self._backend.connect_tcp(
                address.compressed,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            ),
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        return self._backend.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _ServerFetchAsyncNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore async network backend that validates the address it dials."""

    def __init__(self, *, allow_private_networks: bool) -> None:
        self._allow_private_networks = allow_private_networks
        self._backend: httpcore.AsyncNetworkBackend = AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - Signature must match httpcore.
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await _connect_validated_async(
            _validated_connect_addresses(host, port=port, allow_private_networks=self._allow_private_networks),
            lambda address: self._backend.connect_tcp(
                address.compressed,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            ),
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - Signature must match httpcore.
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _connect_validated_sync(
    addresses: list[_IPAddress],
    connect: Callable[[_IPAddress], httpcore.NetworkStream],
) -> httpcore.NetworkStream:
    last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
    for address in addresses:
        try:
            return connect(address)
        except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
            last_error = e
    if last_error is not None:
        raise last_error
    _deny("dns_resolution_failed")


async def _connect_validated_async(
    addresses: list[_IPAddress],
    connect: Callable[[_IPAddress], Awaitable[httpcore.AsyncNetworkStream]],
) -> httpcore.AsyncNetworkStream:
    last_error: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
    for address in addresses:
        try:
            return await connect(address)
        except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
            last_error = e
    if last_error is not None:
        raise last_error
    _deny("dns_resolution_failed")


class ServerFetchHTTPTransport(httpx.HTTPTransport):
    """HTTPX transport that validates server-fetch URLs and dialed addresses."""

    def __init__(self, *, allow_private_networks: bool = False) -> None:
        self._allow_private_networks = allow_private_networks
        self._pool: httpcore.ConnectionPool = httpcore.ConnectionPool(
            network_backend=_ServerFetchSyncNetworkBackend(allow_private_networks=allow_private_networks),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Validate each request before HTTPX sends it."""
        _validate_server_fetch_url(
            str(request.url),
            allow_private_networks=self._allow_private_networks,
            resolve_hostnames=False,
        )
        return super().handle_request(request)

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._pool.close()


class ServerFetchAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """Async HTTPX transport that validates server-fetch URLs and dialed addresses."""

    def __init__(self, *, allow_private_networks: bool = False) -> None:
        self._allow_private_networks = allow_private_networks
        self._pool: httpcore.AsyncConnectionPool = httpcore.AsyncConnectionPool(
            network_backend=_ServerFetchAsyncNetworkBackend(allow_private_networks=allow_private_networks),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Validate each async request before HTTPX sends it."""
        _validate_server_fetch_url(
            str(request.url),
            allow_private_networks=self._allow_private_networks,
            resolve_hostnames=False,
        )
        return await super().handle_async_request(request)

    async def aclose(self) -> None:
        """Close the underlying async connection pool."""
        await self._pool.aclose()
