"""The runtime-owned HTTP client that model provider SDK clients send their requests through."""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Any, Self

import httpx
from httpx._utils import get_environment_proxies

if TYPE_CHECKING:
    from collections.abc import Awaitable

# The connection limits and TCP keep-alive probes the Anthropic SDK gives the client it builds when none is passed
# in; the probes keep long silent requests alive through NAT idle timeouts. macOS names TCP_KEEPIDLE TCP_KEEPALIVE.
_TRANSPORT_OPTIONS: dict[str, Any] = {
    "limits": httpx.Limits(max_connections=1000, max_keepalive_connections=100),
    "socket_options": [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    + [
        (socket.IPPROTO_TCP, getattr(socket, name), value)
        for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPALIVE", 60), ("TCP_KEEPINTVL", 60), ("TCP_KEEPCNT", 5))
        if hasattr(socket, name)
    ],
}
_LOOP_CLIENTS: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


def _loop_client() -> httpx.AsyncClient:
    """Return the running event loop's pooled client, since pooled connections belong to one loop."""
    # A finished loop's client cannot be closed anymore; dropping it lets its idle sockets be collected.
    for finished in [other for other in list(_LOOP_CLIENTS) if other.is_closed()]:
        _LOOP_CLIENTS.pop(finished, None)
    loop = asyncio.get_running_loop()
    client = _LOOP_CLIENTS.get(loop)
    if client is None:
        # A custom transport turns off httpx's proxy environment variables, so mount them as the SDKs do.
        mounts = {
            key: None if url is None else httpx.AsyncHTTPTransport(proxy=url, **_TRANSPORT_OPTIONS)
            for key, url in get_environment_proxies().items()
        }
        transport = httpx.AsyncHTTPTransport(**_TRANSPORT_OPTIONS)
        client = _LOOP_CLIENTS[loop] = httpx.AsyncClient(transport=transport, mounts=mounts, follow_redirects=True)
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


# AGNO_COMPAT: Deep-copied models drop their HTTP client.
# Reason: `Model.__deepcopy__` drops `http_client`, and Learning deep-copies the model every run (see #2976).
# Upstream issue: None identified (searched October 19, 2026).
# Upstream PR: https://github.com/agno-agi/agno/pull/9905 (open) keeps caller-supplied clients on deep copies.
# Remove when: the pinned Agno's model deep copy keeps a caller-supplied `http_client`.
# Coverage: tests/test_model_http_client.py::test_deep_copied_model_keeps_the_shared_http_client.
class KeepHttpClient:
    """Keep a model's HTTP client on its deep copies; mix in ahead of the Agno model class."""

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        """Copy the model as Agno does, then hand the copy the same HTTP client."""
        copied = super().__deepcopy__(memo)  # ty: ignore[unresolved-attribute]  # Agno's Model.__deepcopy__
        copied.http_client = self.http_client  # ty: ignore[unresolved-attribute]
        return copied


# Requests go through the loops' pooled clients, so this one needs no transport or proxy settings of its own.
MODEL_HTTP_CLIENT: httpx.AsyncClient = _ModelHttpClient(transport=httpx.AsyncBaseTransport(), trust_env=False)


async def aclose_model_http_client() -> None:
    """Close the running event loop's pooled model connections before the loop stops."""
    client = _LOOP_CLIENTS.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()


async def closing_model_connections[T](work: Awaitable[T]) -> T:
    """Await work on a loop that finishes afterwards, then close that loop's pooled model connections."""
    try:
        return await work
    finally:
        await aclose_model_http_client()
