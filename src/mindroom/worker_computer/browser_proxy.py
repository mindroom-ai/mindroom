"""Worker-local SOCKS5 CONNECT relay enforcing browser destination policy."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mindroom.server_fetch_url import validated_connect_addresses

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter
    from collections.abc import Mapping

_SETUP_DEADLINE = 10.0
_MAX_CONNECTIONS = 128
COMPUTER_PROXY_BYPASS = (
    "<-loopback>,localhost,localhost.,*.localhost,*.localhost.,127.0.0.0/8,[::1],::ffff:127.0.0.0/104"
)
_REPLY_ADDRESS = b"\x00\x01\x00\x00\x00\x00\x00\x00"


def browser_upstream_proxy_url(runtime_env: Mapping[str, str], worker_env: Mapping[str, str]) -> str | None:
    """Keep one configured browser egress route, failing closed on unsupported modes."""
    settings: dict[str, str] = {}
    for env in (runtime_env, worker_env):
        for name in ("all_proxy", "http_proxy", "https_proxy", "auto_proxy", "socks_server"):
            value = env.get(name) or env.get(name.upper(), env.get(name))
            if value is not None:
                settings[name] = value
    if "auto_proxy" in settings:
        msg = "Computer browser requires all_proxy instead of automatic proxy configuration."
        raise ValueError(msg)
    proxy = settings.get("all_proxy")
    http, https = settings.get("http_proxy"), settings.get("https_proxy")
    if not proxy and http and http == https:
        proxy = http
    if not proxy and (http or https or settings.get("socks_server")):
        msg = "Computer browser requires all_proxy or matching http_proxy and https_proxy settings."
        raise ValueError(msg)
    if proxy:
        parsed = urlsplit(proxy)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            msg = "Computer browser requires an HTTP(S) proxy URL without embedded credentials."
            raise ValueError(msg)
        _ = parsed.port  # Validate malformed ports before launching either provider.
    return proxy or None


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
        """Listen only on an ephemeral worker loopback port."""
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
            if not host or any(character in host for character in "/\\\x00:%[]"):
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
        addresses = await asyncio.to_thread(
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
