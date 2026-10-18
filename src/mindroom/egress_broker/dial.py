"""Guarded upstream dialing with SSRF protection."""

from __future__ import annotations

import asyncio
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache, partial
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.server_fetch_url import ServerFetchUrlError, validated_connect_addresses
from mindroom.worker_computer.browser_proxy import UpstreamTunnelRefusedError, is_loopback, open_upstream_tunnel

if TYPE_CHECKING:
    import ipaddress

    from mindroom.worker_computer.browser_proxy import BrowserEgress

    _IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
    _IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

__all__ = [
    "DestinationBlockedError",
    "DialPolicy",
    "open_upstream",
]


logger = get_logger(__name__)

# Worker code can open many connections to names whose nameservers never answer. Lookups therefore run on their own
# threads, so they cannot exhaust the default executor that rule reads, audit writes, and leaf certificates share, and
# each requester holds at most a few of them at once.
_LOOKUP_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="mindroom-egress-dns")

_TUNNEL_REQUIREMENT = (
    "The egress broker tunnels to the IP address it validated, so the operator proxy must allow CONNECT to IP "
    "addresses on ports 80 and 443; proxies that allow only hostnames are unsupported."
)


@dataclass(frozen=True)
class DialPolicy:
    """Policy for upstream connection dialing.

    ``egress`` holds the operator HTTP(S) proxies (from ``HTTPS_PROXY``, ``HTTP_PROXY``, and ``NO_PROXY``)
    that destinations reach the internet through. The destination guard applies before and regardless of them.
    ``private_networks`` opens private addresses inside them, never loopback.
    """

    allow_private_networks: bool = False
    private_networks: tuple[_IPNetwork, ...] = ()
    allow_loopback: bool = False
    connect_timeout: float = 10.0
    egress: BrowserEgress | None = None


class DestinationBlockedError(Exception):
    """The destination was blocked by validation policy."""


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
        # Dial the validated IP address, not the hostname; asyncio takes its family from the literal without DNS.
        return await asyncio.open_connection(
            address.compressed,
            port,
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


def _lookup_finished(lookup_slots: asyncio.Semaphore, lookup: asyncio.Future[list[_IPAddress]]) -> None:
    lookup_slots.release()
    if not lookup.cancelled():
        # The connection that asked may have stopped waiting for the answer.
        lookup.exception()


async def open_upstream(
    host: str,
    port: int,
    *,
    policy: DialPolicy,
    ssl_context: ssl.SSLContext | None,
    lookup_slots: asyncio.Semaphore,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a validated TCP connection to an upstream host.

    Resolution runs on the broker's lookup threads, holding one of
    `lookup_slots` until its thread returns. The connection
    dials the validated IP address (never the hostname again), trying addresses
    in order with a 1s per-attempt timeout except the last, all within the
    policy's connect_timeout. When the policy names an operator proxy for the
    port, the connection is an HTTP CONNECT tunnel through it to that IP
    instead, except for loopback and NO_PROXY destinations, and TLS (when
    requested) runs inside the tunnel.

    Raises:
        DestinationBlockedError: The destination was blocked by validation policy.
        OSError: The hostname did not resolve, all addresses failed to connect, or the connect_timeout elapsed.
        UpstreamTunnelRefusedError: The operator proxy refused the tunnel; no other address is tried.
        ssl.SSLError: TLS handshake failed (when ssl_context is provided).

    """
    await lookup_slots.acquire()
    lookup = asyncio.get_running_loop().run_in_executor(
        _LOOKUP_EXECUTOR,
        partial(
            validated_connect_addresses,
            host,
            port=port,
            allow_private_networks=policy.allow_private_networks,
            allow_loopback=policy.allow_loopback,
            private_networks=policy.private_networks,
        ),
    )
    lookup.add_done_callback(partial(_lookup_finished, lookup_slots))
    try:
        # A resolver can outlive the connection waiting on it; the slot stays taken until the lookup ends.
        addresses = await asyncio.shield(lookup)
    except ServerFetchUrlError as exc:
        # An unresolvable name is a failed connect, not a policy refusal.
        if exc.reason == "dns_resolution_failed":
            raise OSError(str(exc)) from exc
        raise DestinationBlockedError(str(exc)) from exc

    async with asyncio.timeout(policy.connect_timeout):
        for index, address in enumerate(addresses, start=1):
            last_attempt = index == len(addresses)
            try:
                # The last attempt gets the remaining time from connect_timeout, others get 1s.
                async with asyncio.timeout(None if last_attempt else 1.0):
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
            except OSError:
                if last_attempt:
                    raise
    # Only an empty address list gets here: every attempt returns, or the last one raises.
    msg = "No addresses to connect to"
    raise OSError(msg)
