"""The runtime-owned HTTP client that model provider SDK clients send their requests through."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

# The connection limits the OpenAI and Anthropic SDKs give the client they build when none is passed in.
_SDK_CONNECTION_LIMITS = httpx.Limits(max_connections=1000, max_keepalive_connections=100)
_LOOP_CLIENTS: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


def _loop_client() -> httpx.AsyncClient:
    """Return the running event loop's pooled client, since pooled connections belong to one loop."""
    loop = asyncio.get_running_loop()
    client = _LOOP_CLIENTS.get(loop)
    if client is None:
        # A finished loop's client cannot be closed anymore; dropping it lets its idle sockets be collected.
        for finished in [other for other in list(_LOOP_CLIENTS) if other.is_closed()]:
            _LOOP_CLIENTS.pop(finished, None)
        client = _LOOP_CLIENTS[loop] = httpx.AsyncClient(limits=_SDK_CONNECTION_LIMITS, follow_redirects=True)
    return client


class _ModelHttpClient(httpx.AsyncClient):
    """Send each model request through the running event loop's pooled client.

    Given no client, the provider SDKs build one per model, and its garbage-collection finalizer closes it
    late, after its sockets' descriptors can belong to new connections, which then hang (#2976).
    Models are built in worker threads, so the loop's pool is picked per request. SDK clients close the
    client they were given, as MindRoom does for Claude after each turn, so closing this shared one is a no-op.
    """

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        """Send the request through the running event loop's pool."""
        return await _loop_client().send(request, **kwargs)

    async def aclose(self) -> None:
        """Keep the shared pools open when one model's SDK client closes."""


# Requests go through the loops' pooled clients, so this one needs no transport or proxy settings of its own.
MODEL_HTTP_CLIENT: httpx.AsyncClient = _ModelHttpClient(transport=httpx.AsyncBaseTransport(), trust_env=False)


async def aclose_model_http_client() -> None:
    """Close the running event loop's pooled model connections at runtime shutdown."""
    client = _LOOP_CLIENTS.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()
