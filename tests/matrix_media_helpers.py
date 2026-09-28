"""Test helpers that serve Matrix media downloads the way the bounded sidecar reader requests them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable


@dataclass
class FakeMediaBody:
    """The subset of aiohttp's StreamReader a bounded media download reads."""

    chunks: Iterable[bytes]

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        """Yield the stored chunks as the transport would deliver them."""
        for chunk in self.chunks:
            yield chunk


@dataclass
class FakeMediaResponse:
    """The subset of aiohttp's ClientResponse a bounded media download reads."""

    status: int = 200
    chunks: Iterable[bytes] = ()
    content_length: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    released: bool = field(default=False, init=False)

    @property
    def content(self) -> FakeMediaBody:
        """Return the response body stream."""
        return FakeMediaBody(self.chunks)

    def release(self) -> None:
        """Record that the connection went back to the pool."""
        self.released = True


def media_response(payload: bytes | None) -> FakeMediaResponse:
    """Return a media download answer: the payload with its length, or 404 when it is missing."""
    if payload is None:
        return FakeMediaResponse(status=404)
    return FakeMediaResponse(chunks=[payload], content_length=len(payload))


def requested_mxc(path: str) -> str:
    """Return the MXC URI one media download path requests."""
    server_name, media_id = urlsplit(path).path.split("/download/", 1)[1].split("/", 1)
    return f"mxc://{unquote(server_name)}/{unquote(media_id)}"


def media_send(serve: Callable[[str], bytes | None]) -> Callable[..., Awaitable[FakeMediaResponse]]:
    """Return a ``client.send`` replacement that answers media downloads from ``serve(mxc_url)``."""

    async def send(method: str, path: str, *_args: object, **_kwargs: object) -> FakeMediaResponse:
        assert method == "GET"
        return media_response(serve(requested_mxc(path)))

    return send
