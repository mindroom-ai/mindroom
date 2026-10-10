"""Guarded upstream dialing with SSRF protection."""

from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.server_fetch_url import validated_connect_addresses
from mindroom.worker_computer.browser_proxy import UpstreamTunnelRefusedError, is_loopback, open_upstream_tunnel

if TYPE_CHECKING:
    import ipaddress

    from mindroom.worker_computer.browser_proxy import BrowserEgress

    _IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

__all__ = [
    "DestinationBlockedError",
    "DestinationUnresolvableError",
    "DialPolicy",
    "open_upstream",
]


logger = get_logger(__name__)

_TUNNEL_REQUIREMENT = (
    "The egress broker tunnels to the IP address it validated, so the operator proxy must allow CONNECT to IP "
    "addresses on ports 80 and 443; proxies that allow only hostnames are unsupported."
)


@dataclass(frozen=True)
class DialPolicy:
    """Policy for upstream connection dialing.

    ``egress`` holds the operator HTTP(S) proxies (from ``HTTPS_PROXY``, ``HTTP_PROXY``, and ``NO_PROXY``)
    that destinations reach the internet through. The destination guard applies before and regardless of them.
    """

    allow_private_networks: bool = False
    allow_loopback: bool = False
    connect_timeout: float = 10.0
    egress: BrowserEgress | None = None


class DestinationBlockedError(Exception):
    """The destination was blocked by validation policy."""


class DestinationUnresolvableError(OSError):
    """The destination hostname could not be resolved."""


@cache
def _operator_proxy_tls() -> ssl.SSLContext:
    """Return the context that verifies an ``https://`` operator proxy against the system roots."""
    return ssl.create_default_context()


async def _dial(
    host: str,
    address: _IPAddress,
    port: int,
    *,
    policy: DialPolicy,
    ssl_context: ssl.SSLContext | None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to one validated address, directly or through the operator proxy, then handshake TLS for `host`."""
    upstream = None
    # An operator proxy's loopback is another host, and NO_PROXY names destinations that skip it.
    if (egress := policy.egress) is not None and not is_loopback(address) and not egress.bypasses(host, address):
        upstream = egress.upstream_for(port)
    if upstream is None:
        # Dial the validated IP address, not the hostname
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        return await asyncio.open_connection(
            address.compressed,
            port,
            family=family,
            ssl=ssl_context,
            server_hostname=host if ssl_context else None,
        )
    # The proxy gets only the validated IP and port, so it cannot resolve the name to anything else.
    reader, writer = await open_upstream_tunnel(
        upstream,
        address.compressed,
        port,
        _operator_proxy_tls() if upstream.tls else None,
    )
    if ssl_context is not None:
        try:
            await writer.start_tls(ssl_context, server_hostname=host)
        except BaseException:
            writer.transport.abort()
            raise
    return reader, writer


async def open_upstream(
    host: str,
    port: int,
    *,
    policy: DialPolicy,
    ssl_context: ssl.SSLContext | None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a validated TCP connection to an upstream host.

    Resolution runs in a thread to avoid blocking the event loop. The connection
    dials the validated IP address (never the hostname again), trying addresses
    in order with a 1s per-attempt timeout except the last, all within the
    policy's connect_timeout. When the policy names an operator proxy for the
    port, the connection is an HTTP CONNECT tunnel through it to that IP
    instead, except for loopback and NO_PROXY destinations, and TLS (when
    requested) runs inside the tunnel.

    Raises:
        DestinationBlockedError: The destination was blocked by validation policy.
        DestinationUnresolvableError: The destination hostname could not be resolved.
        OSError: All addresses failed to connect or the connect_timeout elapsed.
        UpstreamTunnelRefusedError: The operator proxy refused the tunnel; no other address is tried.
        ssl.SSLError: TLS handshake failed (when ssl_context is provided).

    """
    # Resolve and validate the destination in a thread
    try:
        addresses: list[_IPAddress] = await asyncio.to_thread(
            validated_connect_addresses,
            host,
            port=port,
            allow_private_networks=policy.allow_private_networks,
            allow_loopback=policy.allow_loopback,
        )
    except ValueError as exc:
        # Check if this is a resolution failure or a validation failure
        # ServerFetchUrlError has a reason attribute that distinguishes these
        if hasattr(exc, "reason") and exc.reason == "dns_resolution_failed":
            raise DestinationUnresolvableError(str(exc)) from exc
        raise DestinationBlockedError(str(exc)) from exc

    if not addresses:
        msg = "No addresses to connect to"
        raise OSError(msg)

    # Try each address in order with timeouts
    last_error: OSError | None = None
    attempt_count = len(addresses)

    async with asyncio.timeout(policy.connect_timeout):
        for index, address in enumerate(addresses):
            is_last_attempt = index == attempt_count - 1
            # Last attempt gets the remaining time from connect_timeout, others get 1s
            attempt_timeout = None if is_last_attempt else 1.0

            try:
                async with asyncio.timeout(attempt_timeout):
                    return await _dial(host, address, port, policy=policy, ssl_context=ssl_context)
            except UpstreamTunnelRefusedError as refused:
                # The operator denied this destination, so dialing around the proxy or retrying is not an option.
                logger.warning(
                    "egress_broker_upstream_proxy_refused_tunnel",
                    destination=f"{address.compressed}:{port}",
                    status=refused.status,
                    requirement=_TUNNEL_REQUIREMENT,
                )
                raise
            except OSError as exc:
                last_error = exc
                if not is_last_attempt:
                    continue
                # All addresses failed
                raise

    # This should not be reached because asyncio.timeout will raise TimeoutError,
    # but provide a fallback for safety
    if last_error:
        raise last_error
    msg = "Connection failed"
    raise OSError(msg)
