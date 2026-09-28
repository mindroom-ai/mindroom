"""Loopback SOCKS5 CONNECT relay enforcing browser destination policy at dial time."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mindroom.browser_fetch_guard import run_browser_dns_lookup
from mindroom.logging_config import get_logger
from mindroom.server_fetch_url import ServerFetchUrlError, validated_connect_addresses

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter
    from collections.abc import Mapping

_SETUP_DEADLINE = 10.0
_MAX_CONNECTIONS = 128
_REPLY_ADDRESS = b"\x00\x01\x00\x00\x00\x00\x00\x00"
# The proxy carries only TCP, so WebRTC must not send UDP (STUN, TURN, or media) around it.
# Chromium ignores the --force-webrtc-ip-handling-policy spelling.
PROXIED_WEBRTC_ONLY_ARG = "--webrtc-ip-handling-policy=disable_non_proxied_udp"
_LOOPBACK_PROXY_BYPASS = (
    "localhost",
    "localhost.",
    "*.localhost",
    "*.localhost.",
    "127.0.0.0/8",
    "[::1]",
    "::ffff:127.0.0.0/104",
)
_LINK_LOCAL_NETWORKS = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10"))
_NO_PROXY_HOSTNAME = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.?")

logger = get_logger(__name__)


@dataclass(frozen=True)
class BrowserUpstreamProxy:
    """One operator egress proxy that carries every browser connection Chromium does not bypass."""

    server: str
    no_proxy: tuple[str, ...] = ()

    def bypass(self, *, allow_loopback: bool, allow_private_networks: bool) -> str:
        """Return Chromium bypass rules; only destinations the browser policy already allows go direct."""
        rules = ["<-loopback>"]
        if allow_loopback or allow_private_networks:
            rules.extend(_LOOPBACK_PROXY_BYPASS)
        if allow_private_networks:
            rules.extend(self.no_proxy)
        return ",".join(rules)


def _env_setting(envs: tuple[Mapping[str, str], ...], name: str) -> str | None:
    """Return one proxy variable, letting later mappings and lowercase spellings win like curl."""
    setting: str | None = None
    for env in envs:
        value = env.get(name) or env.get(name.upper(), env.get(name))
        if value is not None:
            setting = value
    return setting


def _normalized_proxy_url(name: str, value: str) -> str:
    """Return one comparable HTTP(S) proxy URL; the error never repeats the value, which may hold secrets."""
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
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{scheme}://{host}:{port or (443 if scheme == 'https' else 80)}"


def _bypass_network_allowed(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    """Return whether every address a literal NO_PROXY entry covers is one private browsing may dial."""
    if any(network.version == local.version and network.overlaps(local) for local in _LINK_LOCAL_NETWORKS):
        return False
    try:
        for address in {network.network_address, network.broadcast_address}:
            validated_connect_addresses(address.compressed, port=80, allow_private_networks=True)
    except ServerFetchUrlError:
        return False
    return True


def _no_proxy_bypass_rules(no_proxy: str) -> tuple[str, ...]:
    """Translate NO_PROXY into Chromium bypass rules, dropping literal ranges the policy denies."""
    rules: list[str] = []
    for raw_entry in no_proxy.split(","):
        entry = raw_entry.strip().lower()
        try:
            network = ipaddress.ip_network(entry.removeprefix("[").removesuffix("]"), strict=False)
        except ValueError:
            # NO_PROXY names match their subdomains too; entries with ports or other syntax keep the proxy.
            host = entry.removeprefix("*").removeprefix(".")
            if _NO_PROXY_HOSTNAME.fullmatch(host):
                rules.extend((host, f"*.{host}"))
            continue
        if not _bypass_network_allowed(network):
            continue
        address = network.network_address.compressed
        if network.prefixlen != network.max_prefixlen:
            rules.append(f"{address}/{network.prefixlen}")
        else:
            rules.append(f"[{address}]" if network.version == 6 else address)
    return tuple(rules)


def browser_upstream_proxy(
    runtime_env: Mapping[str, str],
    browser_env: Mapping[str, str],
    *,
    egress_control: bool,
) -> BrowserUpstreamProxy | None:
    """Return the operator egress proxy every browser connection must use, or None for the destination relay.

    One HTTP(S) proxy, from ``all_proxy`` or from ``http_proxy`` and ``https_proxy`` naming the same proxy, carries
    every connection. ``egress_control`` marks a sandbox runner, where such a proxy may be what enforces approved
    egress: there an environment that names no single proxy fails closed. Elsewhere no MindRoom egress policy depends
    on the proxy, so the browser uses the destination relay, which still enforces the browser's own policy.
    """
    envs = (runtime_env, browser_env)
    no_proxy = _env_setting(envs, "no_proxy") or ""
    if any(entry.strip() == "*" for entry in no_proxy.split(",")):
        return None
    proxies = {
        name: _normalized_proxy_url(name, value)
        for name in ("all_proxy", "http_proxy", "https_proxy")
        if (value := _env_setting(envs, name))
    }
    ambiguity: str | None = None
    server: str | None = None
    if _env_setting(envs, "auto_proxy") is not None:
        ambiguity = "auto_proxy selects proxies through a script"
    elif "all_proxy" in proxies:
        server = proxies["all_proxy"]
    elif len(set(proxies.values())) == 1:
        server = next(iter(proxies.values()))
    elif proxies:
        ambiguity = "http_proxy and https_proxy name different proxies"
    elif _env_setting(envs, "socks_server"):
        msg = "Browser supports only HTTP(S) egress proxies, but socks_server names a SOCKS proxy."
        raise ValueError(msg)
    if ambiguity is not None:
        if egress_control:
            msg = f"Browser cannot choose one egress proxy because {ambiguity}; set all_proxy to the proxy to use."
            raise ValueError(msg)
        logger.warning("browser_egress_proxy_ambiguous_using_destination_relay", reason=ambiguity)
        return None
    if server is None:
        return None
    return BrowserUpstreamProxy(server=server, no_proxy=_no_proxy_bypass_rules(no_proxy))


class BrowserDestinationProxy:
    """Validate each TCP destination, including redirects invisible to page routes."""

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        allow_loopback: bool = False,
    ) -> None:
        self.endpoint = ""
        self._allow_private_networks = allow_private_networks
        self._allow_loopback = allow_loopback
        self._server: asyncio.Server | None = None
        self._port = 0
        self._connections: dict[asyncio.Task[None], StreamWriter] = {}

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

    async def _connect(self, host: str, port: int) -> tuple[StreamReader, StreamWriter]:
        addresses = await run_browser_dns_lookup(
            validated_connect_addresses,
            host,
            port=port,
            allow_private_networks=self._allow_private_networks,
            allow_loopback=self._allow_loopback,
        )
        if port == self._port and any(
            address.is_loopback
            or (
                isinstance(address, ipaddress.IPv6Address)
                and address.ipv4_mapped is not None
                and address.ipv4_mapped.is_loopback
            )
            for address in addresses
        ):
            msg = "Browser proxy cannot connect to itself."
            raise ValueError(msg)
        for address in addresses:
            try:
                return await asyncio.open_connection(
                    address.compressed,
                    port,
                    family=socket.AF_INET if address.version == 4 else socket.AF_INET6,
                )
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
            writer.write_eof()
        except OSError:
            writer.transport.abort()
