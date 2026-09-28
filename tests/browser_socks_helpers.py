"""SOCKS5 client helper for tests that drive a browser destination relay directly."""

from __future__ import annotations

import asyncio
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
