"""Answer guarded fetches in tests without a network, after MindRoom's transport has checked each URL."""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import pytest

PUBLIC_ADDRESS = "93.184.216.34"


def public_dns(monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...] = (PUBLIC_ADDRESS,)) -> None:
    """Resolve every hostname to `addresses`, in order."""

    def resolve(_host: str, port: int, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port)) for address in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


class TrickleBody(httpx.AsyncByteStream):
    """A response body that arrives one byte every 20 ms for as long as it is read."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield one byte at a time, without end."""
        while True:
            await asyncio.sleep(0.02)
            yield b"x"


def serve(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Answer every request that passed MindRoom's transports with `handler`, and record it."""
    requests: list[httpx.Request] = []

    async def respond(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    return requests


def serve_routes(monkeypatch: pytest.MonkeyPatch, routes: dict[str, httpx.Response]) -> list[httpx.Request]:
    """Answer each URL in `routes` with its response's raw body, unread, and any other URL with a 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(str(request.url), httpx.Response(404))
        return httpx.Response(route.status_code, headers=route.headers, stream=route.stream)

    return serve(monkeypatch, handler)
