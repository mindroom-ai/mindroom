"""Relay one WebSocket message by message between an accepted client socket and an aiohttp client socket."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

import aiohttp
from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


# The protocol's close codes a close frame may carry (RFC 6455 7.4); applications add 3000 to 4999.
_SENDABLE_CLOSE_CODES = frozenset({1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014})


def _close_code(code: int | None) -> int:
    """Return the close code to pass on: one a close frame may carry, 1000 for a close without one, else 1011."""
    # A close frame without a code reads as 1005 from the caller and 0 from the app.
    if code in {None, 0, 1005}:
        return 1000
    # 1006 and the other codes no frame carries stand for a lost connection.
    return code if code in _SENDABLE_CLOSE_CODES or 3000 <= code <= 4999 else 1011


async def _client_to_upstream(websocket: WebSocket, upstream: aiohttp.ClientWebSocketResponse) -> int:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return _close_code(message.get("code"))
        if message.get("bytes") is not None:
            await upstream.send_bytes(message["bytes"])
        elif message.get("text") is not None:
            await upstream.send_str(message["text"])


async def _upstream_to_client(websocket: WebSocket, upstream: aiohttp.ClientWebSocketResponse) -> int:
    while True:
        message = await upstream.receive()
        if message.type == aiohttp.WSMsgType.TEXT:
            await websocket.send_text(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await websocket.send_bytes(message.data)
        else:
            # A close frame carries its own code, which aiohttp replaces once the connection drops; else it was lost.
            return _close_code(message.data if message.type == aiohttp.WSMsgType.CLOSE else upstream.close_code)


async def relay_websocket(websocket: WebSocket, upstream: aiohttp.ClientWebSocketResponse) -> None:
    """Pass messages both ways until either side closes, then close both with that side's close code."""
    tasks = [
        asyncio.create_task(_client_to_upstream(websocket, upstream)),
        asyncio.create_task(_upstream_to_client(websocket, upstream)),
    ]
    code = 1000
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            with suppress(WebSocketDisconnect, aiohttp.ClientError, OSError, RuntimeError):
                code = task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(aiohttp.ClientError, OSError, RuntimeError):
            await upstream.close(code=code)
        with suppress(RuntimeError, OSError):
            await websocket.close(code=code)
