"""SOCKS5 client and egress-proxy fakes for tests of the browser destination relay."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from urllib.parse import urlsplit


async def socks5_connect(
    endpoint: str,
    host: str,
    port: int,
    *,
    literal: bool = False,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, int]:
    """Send one SOCKS5 CONNECT to ``endpoint`` and return the stream and reply code."""
    relay = urlsplit(endpoint)
    reader, writer = await asyncio.open_connection(relay.hostname, relay.port)
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    assert await reader.readexactly(2) == b"\x05\x00"
    if literal:
        address = ipaddress.ip_address(host)
        encoded = bytes([1 if address.version == 4 else 4]) + address.packed
    else:
        hostname = host.encode("ascii")
        encoded = b"\x03" + bytes([len(hostname)]) + hostname
    writer.write(b"\x05\x01\x00" + encoded + port.to_bytes(2, "big"))
    await writer.drain()
    reply = await reader.readexactly(10)
    return reader, writer, reply[1]


# Squid's default and older MindRoom egress-proxy images: `http_access deny CONNECT !SSL_ports` with `port 443`.
SQUID_DEFAULT_CONNECT_PORTS = frozenset({443})
# MindRoom egress-proxy images that allow CONNECT to plain-HTTP ports as well.
EGRESS_PROXY_CONNECT_PORTS = frozenset({80, 443})


class SquidLikeUpstream:
    """An egress proxy with Squid's CONNECT semantics that serves a fixed page inside each allowed tunnel.

    It refuses CONNECT to a port outside ``connect_ports`` and, when ``allowed_hosts`` is set, to any other target,
    such as the IP literal a primary browser tunnels to, with ``403 Forbidden`` like ``TCP_DENIED/403``.
    """

    def __init__(
        self,
        *,
        connect_ports: frozenset[int] = EGRESS_PROXY_CONNECT_PORTS,
        allowed_hosts: frozenset[str] | None = None,
        title: str = "Via egress proxy",
    ) -> None:
        self.requests: list[bytes] = []
        self._connect_ports = connect_ports
        self._allowed_hosts = allowed_hosts
        self._title = title
        self._server: asyncio.Server | None = None

    async def start(self) -> str:
        """Listen on loopback and return the proxy URL."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def close(self) -> None:
        """Stop listening."""
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError, OSError, ValueError):
            request_line = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0]
            self.requests.append(request_line)
            method, authority, _version = request_line.decode("ascii").split(" ", 2)
            host, _, port = authority.rpartition(":")
            if (
                method != "CONNECT"
                or int(port) not in self._connect_ports
                or (self._allowed_hosts is not None and host.strip("[]") not in self._allowed_hosts)
            ):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            else:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await reader.readuntil(b"\r\n\r\n")
                body = f"<title>{self._title}</title>".encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        writer.close()
