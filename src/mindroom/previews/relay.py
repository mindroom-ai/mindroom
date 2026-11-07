"""Relay one WebSocket message by message between an accepted client socket and an aiohttp client socket."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

import aiohttp
from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


def _close_code(code: int | None) -> int:
    """Return a close code either side may send: an application's 3000 to 4999, else a normal 1000."""
    return code if code is not None and 3000 <= code <= 4999 else 1000


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
    async for message in upstream:
        if message.type == aiohttp.WSMsgType.TEXT:
            await websocket.send_text(message.data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            await websocket.send_bytes(message.data)
        else:
            break
    return _close_code(upstream.close_code)


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
