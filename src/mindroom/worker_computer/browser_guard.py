"""Worker-owned authenticated loopback verifier for intercepted browser requests."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import secrets
from typing import TYPE_CHECKING

from mindroom.browser_fetch_guard import validate_browser_fetch_url
from mindroom.server_fetch_url import ServerFetchUrlError

if TYPE_CHECKING:
    from asyncio import StreamReader, StreamWriter

_MAX_BODY = 16 * 1024
_MAX_URL = 8 * 1024
_DEADLINE = 10.0


class BrowserURLVerifier:
    """Keep immutable network policy outside model arguments and browser JavaScript."""

    def __init__(self, *, allow_private_networks: bool = False) -> None:
        self.token = secrets.token_urlsafe(32)
        self.endpoint = ""
        self._allow_private_networks = allow_private_networks
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Bind a private ephemeral loopback endpoint before MCP startup."""
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=_MAX_BODY)
        port = self._server.sockets[0].getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{port}/verify"

    async def close(self) -> None:
        """Close listener and pending callbacks before retiring this session."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        tasks = tuple(self._connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle(self, reader: StreamReader, writer: StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        try:
            async with asyncio.timeout(_DEADLINE):
                status, allowed = await self._verify_request(reader)
                body = json.dumps({"allowed": allowed}).encode()
                writer.write(
                    f"HTTP/1.1 {status} Response\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body,
                )
                await writer.drain()
        except (TimeoutError, ValueError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self._connections.discard(task)

    async def _verify_request(self, reader: StreamReader) -> tuple[int, bool]:  # noqa: C901, PLR0911 - fail-closed HTTP parser
        raw = await reader.readuntil(b"\r\n\r\n")
        if len(raw) > _MAX_BODY:
            return 400, False
        lines = raw.decode("ascii").split("\r\n")
        if lines[0] != "POST /verify HTTP/1.1":
            return 400, False
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key in headers:
                return 400, False
            headers[key] = value.strip()
        if not hmac.compare_digest(headers.get("authorization", ""), "Bearer " + self.token):
            return 403, False
        if "transfer-encoding" in headers or headers.get("content-type") != "application/json":
            return 400, False
        size = int(headers.get("content-length", "0"))
        if not 0 < size <= _MAX_BODY:
            return 400, False
        value = json.loads(await reader.readexactly(size))
        if not isinstance(value, dict) or set(value) != {"url"}:
            return 400, False
        url = value["url"]
        if not isinstance(url, str) or not 0 < len(url.encode()) <= _MAX_URL:
            return 200, False
        try:
            await asyncio.to_thread(
                validate_browser_fetch_url,
                url,
                allow_private_networks=self._allow_private_networks,
            )
        except (ServerFetchUrlError, ValueError, OSError):
            return 200, False
        return 200, True
