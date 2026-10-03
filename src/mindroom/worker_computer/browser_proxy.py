"""Loopback SOCKS5 CONNECT relay enforcing browser destination policy at dial time."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mindroom.browser_fetch_guard import run_browser_dns_lookup
from mindroom.logging_config import get_logger
from mindroom.server_fetch_url import validated_connect_addresses

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter
    from collections.abc import Mapping

    _IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_SETUP_DEADLINE = 10.0
_MAX_CONNECTIONS = 128
# One page cannot hold more of the shared browser DNS threads than this, even after its lookups time out.
_MAX_CONCURRENT_LOOKUPS = 4
_MAX_UPSTREAM_RESPONSE_HEAD = 16 * 1024
_REPLY_ADDRESS = b"\x00\x01\x00\x00\x00\x00\x00\x00"
# The relay carries only TCP, so WebRTC must not send UDP (STUN, TURN, or media) around it.
# Chromium ignores the --force-webrtc-ip-handling-policy spelling.
PROXIED_WEBRTC_ONLY_ARG = "--webrtc-ip-handling-policy=disable_non_proxied_udp"
# Chromium bypasses a proxy for loopback unless told otherwise; the relay must see those connections too.
RELAY_ONLY_PROXY_BYPASS = "<-loopback>"

logger = get_logger(__name__)


@dataclass(frozen=True)
class _UpstreamProxy:
    """One operator HTTP(S) proxy the relay opens CONNECT tunnels through."""

    host: str
    port: int
    tls: bool


@dataclass(frozen=True)
class BrowserEgress:
    """Where the relay sends destinations its policy allows.

    Connections to port 80 use ``http``, every other port uses ``https``, and either may be None for a direct dial.
    ``by_hostname`` marks a sandbox runner, whose approved-egress proxy needs destination names and resolves them
    itself, so rebinding between the relay's check and that proxy's lookup is the proxy's responsibility.
    Elsewhere the relay tunnels to the address it validated.
    """

    http: _UpstreamProxy | None = None
    https: _UpstreamProxy | None = None
    no_proxy: tuple[str, ...] = ()
    by_hostname: bool = False

    def _upstream_for(self, port: int) -> _UpstreamProxy | None:
        """Return the upstream proxy for one destination port, like curl's per-scheme proxy variables."""
        return self.http if port == 80 else self.https

    def _bypasses(self, host: str, address: _IPAddress) -> bool:
        """Return whether NO_PROXY names this already-validated destination."""
        name = host.rstrip(".").lower()
        candidates = [address]
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            candidates.append(address.ipv4_mapped)
        for entry in self.no_proxy:
            if entry == "*":
                return True
            try:
                network = ipaddress.ip_network(entry.removeprefix("[").removesuffix("]"), strict=False)
            except ValueError:
                domain = entry.removeprefix("*").removeprefix(".").rstrip(".")
                if domain and (name == domain or name.endswith(f".{domain}")):
                    return True
                continue
            if any(candidate.version == network.version and candidate in network for candidate in candidates):
                return True
        return False


def _env_setting(envs: tuple[Mapping[str, str], ...], name: str) -> str | None:
    """Return one proxy variable, letting later mappings and lowercase spellings win like curl."""
    setting: str | None = None
    for env in envs:
        value = env.get(name) or env.get(name.upper(), env.get(name))
        if value is not None:
            setting = value
    return setting


def _upstream_proxy(name: str, value: str) -> _UpstreamProxy:
    """Parse one HTTP(S) proxy URL; the error never repeats the value, which may hold secrets."""
    raw = value.strip()
    try:
        parsed = urlsplit(raw if "://" in raw else f"http://{raw}")
        port = parsed.port
    except ValueError:
        msg = f"Browser cannot use {name}: it is not a valid proxy URL."
        raise ValueError(msg) from None
    scheme = parsed.scheme.lower()
    if scheme.startswith("socks"):
        msg = f"Browser supports only HTTP(S) egress proxies, but {name} names a SOCKS proxy."
        raise ValueError(msg)
    if scheme not in {"http", "https"} or not parsed.hostname:
        msg = f"Browser requires {name} to be an http:// or https:// proxy URL."
        raise ValueError(msg)
    if parsed.username is not None or parsed.password is not None:
        msg = f"Browser cannot use {name}: proxy credentials inside the URL are not supported."
        raise ValueError(msg)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        msg = f"Browser cannot use {name}: a proxy URL must not include a path, query, or fragment."
        raise ValueError(msg)
    return _UpstreamProxy(host=parsed.hostname, port=port or (443 if scheme == "https" else 80), tls=scheme == "https")


def browser_egress(
    runtime_env: Mapping[str, str],
    browser_env: Mapping[str, str],
    *,
    egress_control: bool,
) -> BrowserEgress:
    """Choose the upstream proxies behind the destination relay from the proxy environment.

    ``egress_control`` marks a sandbox runner, where a configured proxy may be what enforces approved egress.
    There every configured proxy variable must name one supported HTTP(S) proxy, which then carries every
    connection, and anything else fails closed. Elsewhere no MindRoom egress policy depends on the proxy, so the
    variables follow curl precedence, and an unsupported proxy is ignored with a warning in favor of a direct dial,
    which the relay still confines to the browser's destination policy.
    """
    envs = (runtime_env, browser_env)
    no_proxy = tuple(
        entry for raw in (_env_setting(envs, "no_proxy") or "").split(",") if (entry := raw.strip().lower())
    )
    configured = {
        name: value for name in ("all_proxy", "http_proxy", "https_proxy") if (value := _env_setting(envs, name))
    }
    unsupported = [name for name in ("auto_proxy", "socks_server") if _env_setting(envs, name) is not None]
    if egress_control:
        if unsupported:
            msg = f"Browser cannot follow {unsupported[0]}; set all_proxy to the one HTTP(S) egress proxy to use."
            raise ValueError(msg)
        proxies = {_upstream_proxy(name, value) for name, value in configured.items()}
        if len(proxies) > 1:
            msg = "Browser cannot choose one egress proxy because the proxy variables name different proxies."
            raise ValueError(msg)
        upstream = next(iter(proxies), None)
        return BrowserEgress(http=upstream, https=upstream, no_proxy=no_proxy, by_hostname=True)
    for name in unsupported:
        logger.warning("browser_proxy_variable_ignored", variable=name)

    def scheme_proxy(name: str) -> _UpstreamProxy | None:
        used = name if name in configured else "all_proxy"
        if used not in configured:
            return None
        try:
            return _upstream_proxy(used, configured[used])
        except ValueError as exc:
            logger.warning("browser_upstream_proxy_unsupported_dialing_directly", variable=used, reason=str(exc))
            return None

    return BrowserEgress(http=scheme_proxy("http_proxy"), https=scheme_proxy("https_proxy"), no_proxy=no_proxy)


_PRIMARY_TUNNEL_REQUIREMENT = (
    "The primary browser tunnels to the IP address it validated, so its egress proxy must allow CONNECT to IP "
    "addresses on ports 80 and 443; proxies that allow only hostnames are unsupported for the primary browser."
)
_RUNNER_TUNNEL_REQUIREMENT = (
    "Browsers tunnel every connection, plain HTTP included, with CONNECT, so the egress proxy must allow CONNECT "
    "to the allowed hostnames on ports 80 and 443."
)


class _UpstreamTunnelRefusedError(OSError):
    """The operator's egress proxy denied a destination, so no other address or route is tried."""

    def __init__(self, status: str) -> None:
        super().__init__(f"Browser upstream proxy refused the tunnel with HTTP {status}.")
        self.status = status


async def _open_upstream_tunnel(
    upstream: _UpstreamProxy,
    target: str,
    port: int,
    tls: ssl.SSLContext | None,
) -> tuple[StreamReader, StreamWriter]:
    """Open one HTTP CONNECT tunnel through an operator proxy."""
    reader, writer = await asyncio.open_connection(
        upstream.host,
        upstream.port,
        ssl=tls if upstream.tls else None,
        limit=_MAX_UPSTREAM_RESPONSE_HEAD,
    )
    try:
        authority = f"[{target}]:{port}" if ":" in target else f"{target}:{port}"
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii"))
        await writer.drain()
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        writer.transport.abort()
        msg = "Browser upstream proxy closed the tunnel."
        raise OSError(msg) from exc
    except BaseException:
        writer.transport.abort()
        raise
    status = head.split(b"\r\n", 1)[0].split(b" ")
    if len(status) >= 2 and status[0].startswith(b"HTTP/1.") and status[1].startswith(b"2"):
        return reader, writer
    writer.transport.abort()
    if len(status) >= 2 and status[1].startswith(b"4"):
        raise _UpstreamTunnelRefusedError(status[1].decode("ascii", "replace"))
    msg = "Browser upstream proxy could not open the tunnel."
    raise OSError(msg)


def _is_loopback(address: _IPAddress) -> bool:
    return address.is_loopback or (
        isinstance(address, ipaddress.IPv6Address)
        and address.ipv4_mapped is not None
        and address.ipv4_mapped.is_loopback
    )


class BrowserDestinationProxy:
    """Validate each TCP destination, including redirects invisible to page routes."""

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        allow_loopback: bool = False,
        egress: BrowserEgress | None = None,
    ) -> None:
        self.endpoint = ""
        self._allow_private_networks = allow_private_networks
        self._allow_loopback = allow_loopback
        self._egress = egress or BrowserEgress()
        upstreams = (self._egress.http, self._egress.https)
        self._upstream_tls = (
            ssl.create_default_context() if any(proxy is not None and proxy.tls for proxy in upstreams) else None
        )
        self._server: asyncio.Server | None = None
        self._port = 0
        self._connections: dict[asyncio.Task[None], StreamWriter] = {}
        self._lookup_slots = asyncio.Semaphore(_MAX_CONCURRENT_LOOKUPS)
        self._lookups: dict[tuple[str, int], asyncio.Future[list[_IPAddress]]] = {}

    async def start(self) -> None:
        """Listen only on an ephemeral loopback port of the process that owns the browser."""
        if self._server is None:
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
            self._port = self._server.sockets[0].getsockname()[1]
            self.endpoint = f"socks5://127.0.0.1:{self._port}"

    def _accept(self, reader: StreamReader, writer: StreamWriter) -> None:
        # Register synchronously so close also owns clients accepted this tick.
        if self._server is None or len(self._connections) >= _MAX_CONNECTIONS:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._connections[task] = writer
        task.add_done_callback(self._connections.pop)

    async def close(self) -> None:
        """Close the listener and all handshake/relay tasks without draining peers."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
        tasks = tuple(self._connections)
        for task in tasks:
            self._connections[task].transport.abort()
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    async def _handle(self, reader: StreamReader, writer: StreamWriter) -> None:
        upstream: StreamWriter | None = None
        try:
            async with asyncio.timeout(_SETUP_DEADLINE):
                host, port = await self._handshake(reader, writer)
                try:
                    remote, upstream = await self._connect(host, port)
                except (ValueError, OSError):
                    writer.write(b"\x05\x02" + _REPLY_ADDRESS)
                    await writer.drain()
                    return
                writer.write(b"\x05\x00" + _REPLY_ADDRESS)
                await writer.drain()
            async with asyncio.TaskGroup() as group:
                group.create_task(self._relay(reader, upstream))
                group.create_task(self._relay(remote, writer))
        except (ValueError, OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            # abort() cannot hang behind a peer that stopped reading queued bytes.
            writer.transport.abort()
            if upstream is not None:
                upstream.transport.abort()

    async def _handshake(self, reader: StreamReader, writer: StreamWriter) -> tuple[str, int]:
        version, count = await reader.readexactly(2)
        if version != 5 or count == 0 or 0 not in await reader.readexactly(count):
            msg = "Unsupported browser proxy authentication."
            raise ValueError(msg)
        writer.write(b"\x05\x00")
        await writer.drain()
        version, command, reserved, kind = await reader.readexactly(4)
        if (version, command, reserved) != (5, 1, 0):
            msg = "Only SOCKS5 CONNECT is supported."
            raise ValueError(msg)
        if kind == 3:
            size = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(size)).decode("ascii")
            try:
                # Chromium names IPv6 URL hosts unbracketed; zone-scoped addresses stay refused.
                literal: ipaddress.IPv6Address | None = ipaddress.IPv6Address(host)
            except ValueError:
                literal = None
            if literal is not None and literal.scope_id is None:
                host = literal.compressed
            elif not host or any(character in host for character in "/\\\x00:%[]"):
                msg = "Invalid browser proxy hostname."
                raise ValueError(msg)
        elif kind in (1, 4):
            host = str(ipaddress.ip_address(await reader.readexactly(4 if kind == 1 else 16)))
        else:
            msg = "Unsupported browser proxy address."
            raise ValueError(msg)
        port = int.from_bytes(await reader.readexactly(2), "big")
        if port == 0:
            msg = "Invalid browser proxy port."
            raise ValueError(msg)
        return host, port

    async def _resolve(self, host: str, port: int) -> list[_IPAddress]:
        """Validate and pin one destination, holding a lookup slot until its thread really finishes.

        Connections to a destination whose lookup is running share it, so one slow name takes one slot.
        """
        key = (host, port)
        if key not in self._lookups:
            await self._lookup_slots.acquire()
            if key in self._lookups:
                # Another connection started this lookup while this one waited for a slot.
                self._lookup_slots.release()
            else:
                lookup = asyncio.ensure_future(
                    run_browser_dns_lookup(
                        validated_connect_addresses,
                        host,
                        port=port,
                        allow_private_networks=self._allow_private_networks,
                        allow_loopback=self._allow_loopback,
                    ),
                )
                lookup.add_done_callback(partial(self._lookup_finished, key))
                self._lookups[key] = lookup
        # A resolver can outlive the setup deadline; the slot stays taken until the lookup ends.
        return await asyncio.shield(self._lookups[key])

    def _lookup_finished(self, key: tuple[str, int], lookup: asyncio.Future[list[_IPAddress]]) -> None:
        del self._lookups[key]
        self._lookup_slots.release()
        if not lookup.cancelled():
            # The connection that asked may already have timed out and stopped waiting.
            lookup.exception()

    def _dials_directly(self, host: str, address: _IPAddress, upstream: _UpstreamProxy | None) -> bool:
        # An upstream proxy's loopback is another host, and NO_PROXY applies only to trusted private browsing.
        return (
            upstream is None
            or _is_loopback(address)
            or (self._allow_private_networks and self._egress._bypasses(host, address))
        )

    async def _connect(self, host: str, port: int) -> tuple[StreamReader, StreamWriter]:
        addresses = await self._resolve(host, port)
        if port == self._port and any(_is_loopback(address) for address in addresses):
            msg = "Browser proxy cannot connect to itself."
            raise ValueError(msg)
        upstream = self._egress._upstream_for(port)
        tunnel_targets: set[str] = set()
        for address in addresses:
            try:
                if self._dials_directly(host, address, upstream):
                    return await asyncio.open_connection(
                        address.compressed,
                        port,
                        family=socket.AF_INET if address.version == 4 else socket.AF_INET6,
                    )
                assert upstream is not None
                # Tunnel to the validated address, so the upstream cannot resolve the name to something else.
                target = host if self._egress.by_hostname else address.compressed
                if target not in tunnel_targets:
                    tunnel_targets.add(target)
                    return await _open_upstream_tunnel(upstream, target, port, self._upstream_tls)
            except _UpstreamTunnelRefusedError as refused:
                logger.warning(
                    "browser_egress_proxy_refused_tunnel",
                    destination=f"{target}:{port}",
                    status=refused.status,
                    requirement=_RUNNER_TUNNEL_REQUIREMENT if self._egress.by_hostname else _PRIMARY_TUNNEL_REQUIREMENT,
                )
                raise
            except OSError:
                continue
        msg = "Browser proxy connection failed."
        raise OSError(msg)

    @staticmethod
    async def _relay(reader: StreamReader, writer: StreamWriter) -> None:
        try:
            while data := await reader.read(64 * 1024):
                writer.write(data)
                await writer.drain()
            # TLS to an upstream proxy cannot half-close; the tunnel then ends with the other direction.
            if writer.can_write_eof():
                writer.write_eof()
        except OSError:
            writer.transport.abort()
