"""Guarded upstream dialing with SSRF protection."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.server_fetch_url import validated_connect_addresses

if TYPE_CHECKING:
    import ipaddress
    import ssl

    _IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

__all__ = [
    "DestinationBlockedError",
    "DestinationUnresolvableError",
    "DialPolicy",
    "open_upstream",
]


@dataclass(frozen=True)
class DialPolicy:
    """Policy for upstream connection dialing."""

    allow_private_networks: bool = False
    allow_loopback: bool = False
    connect_timeout: float = 10.0


class DestinationBlockedError(Exception):
    """The destination was blocked by validation policy."""


class DestinationUnresolvableError(OSError):
    """The destination hostname could not be resolved."""


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
    policy's connect_timeout.

    Raises:
        DestinationBlockedError: The destination was blocked by validation policy.
        DestinationUnresolvableError: The destination hostname could not be resolved.
        OSError: All addresses failed to connect or the connect_timeout elapsed.
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
                    # Dial the validated IP address, not the hostname
                    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
                    reader, writer = await asyncio.open_connection(
                        address.compressed,
                        port,
                        family=family,
                        ssl=ssl_context,
                        server_hostname=host if ssl_context else None,
                    )
                    return reader, writer
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
