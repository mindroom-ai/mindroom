"""Relay one WebSocket message by message between an accepted client socket and an aiohttp client socket.

An aiohttp session that opens such a WebSocket uses ``no_redirects``, so a redirected handshake reaches its caller.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

import aiohttp
from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


# The statuses aiohttp follows a redirect on.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# The protocol's close codes a close frame may carry (RFC 6455 7.4); applications add 3000 to 4999.
_SENDABLE_CLOSE_CODES = frozenset({1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014})


class HandshakeRedirected(aiohttp.WSServerHandshakeError):
    """A WebSocket handshake the server answered with a redirect, which ``no_redirects`` keeps aiohttp from following."""


async def no_redirects(request: aiohttp.ClientRequest, handler: aiohttp.ClientHandlerType) -> aiohttp.ClientResponse:
    """Raise a redirect as ``HandshakeRedirected``: a middleware for the session that opens a WebSocket.

    ``ws_connect`` follows every redirect, to any host and with every header but the credentials aiohttp knows of.
    """
    response = await handler(request)
    if response.status in _REDIRECT_STATUSES:
        response.close()
        raise HandshakeRedirected(
            response.request_info,
            (),
            status=response.status,
            message="Redirected",
            headers=response.headers,
        )
    return response


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
    """Pass messages both ways until either side closes or is lost, then close both with the code that ended it.

    Cancelled, it closes only ``upstream``, so whatever cancelled it closes ``websocket`` with its own code.
    """
    tasks = [
        asyncio.create_task(_client_to_upstream(websocket, upstream)),
        asyncio.create_task(_upstream_to_client(websocket, upstream)),
    ]
    code = 1000
    cancelled = False
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                code = task.result()
            except (WebSocketDisconnect, aiohttp.ClientError, OSError, RuntimeError):
                # A side that could not be sent to was lost.
                code = 1011
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(aiohttp.ClientError, OSError, RuntimeError):
            await upstream.close(code=code)
        if not cancelled:
            with suppress(RuntimeError, OSError):
                await websocket.close(code=code)
