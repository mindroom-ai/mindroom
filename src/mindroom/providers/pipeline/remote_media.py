"""Download the media URLs a route does not send, in one pipeline stage inside the media fallback.

Tools such as image generators return media as URLs. A route sends the URLs of the kinds in ``Capabilities.url_media``
for the provider to fetch; this stage downloads every other URL, up to one size cap, and sends its bytes instead. A URL
that cannot be downloaded raises ``InvalidRequest``, and a provider that cannot fetch a URL rejects the request, so the
media fallback replaces the request's media with its note instead of the model answering about media it never saw.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import httpx2

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.llm.errors import InvalidRequest
from mindroom.llm.media import Audio, File, Image, Video, canonical_mime_type, named_by_resource, url_file_name
from mindroom.logging_config import get_logger
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.redaction import redact_url_credentials
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import MediaKind, ModelProvider, ModelRequest, StreamEvent

__all__ = ["remote_media_stream"]

logger = get_logger(__name__)

_DOWNLOAD_TIMEOUT_SECONDS = 30.0
# Media at a URL larger than this gets the media fallback's note instead of being sent.
_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024


async def remote_media_stream(inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` with every media URL ``inner`` does not send replaced by the bytes behind it.

    Messages without such a URL pass as they are; the others are copies, so the caller's messages never change.
    """
    url_media = inner.capabilities.url_media
    provider = inner.identity.provider_name
    messages = [await _with_downloads(message, url_media, provider) for message in request.messages]
    events = inner.stream(replace(request, messages=messages))
    async with closing_async_stream(events):
        async for event in events:
            yield event


async def _with_downloads(message: Message, url_media: frozenset[MediaKind], provider: str) -> Message:
    images = await _downloaded_all(message.images, "image", url_media, provider)
    audio = await _downloaded_all(message.audio, "audio", url_media, provider)
    videos = await _downloaded_all(message.videos, "video", url_media, provider)
    files = await _downloaded_all(message.files, "file", url_media, provider)
    if images is message.images and audio is message.audio and videos is message.videos and files is message.files:
        return message
    return replace(message, images=images, audio=audio, videos=videos, files=files)


async def _downloaded_all[T: (Image, Audio, Video, File)](
    items: Sequence[T] | None,
    kind: MediaKind,
    url_media: frozenset[MediaKind],
    provider: str,
) -> Sequence[T] | None:
    """Return ``items``, or a list with every URL among them downloaded unless the route sends ``kind`` by URL."""
    if not items or kind in url_media or all(item.url is None for item in items):
        return items
    return [await _downloaded(item, kind, provider) for item in items]


async def _downloaded[T: (Image, Audio, Video, File)](item: T, kind: MediaKind, provider: str) -> T:
    """Return ``item`` with the bytes at its URL in place of the URL, keeping the type the URL named for it."""
    url = item.url
    if url is None:
        return item
    data, declared = await _download(url, kind, provider)
    return replace(named_by_resource(item, url_file_name(url), declared), url=None, content=data)


async def _download(url: str, kind: MediaKind, provider: str) -> tuple[bytes, str | None]:
    """Return the bytes at ``url`` and the MIME type the server declared, reading at most the download limit.

    The download goes through the model requests' client, so it trusts the same certificates, uses the same proxies,
    and follows redirects.
    """
    try:
        async with MODEL_HTTP_CLIENT.stream("GET", url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            data = await collect_bounded_bytes(response.aiter_bytes(), max_bytes=_DOWNLOAD_LIMIT_BYTES)
            declared = canonical_mime_type(response.headers.get("content-type"))
    except (httpx2.HTTPError, httpx2.InvalidURL, ByteLimitExceededError) as error:
        _log_unavailable(url, kind, error)
        msg = f"The {kind} at a remote URL could not be downloaded for the model"
        raise InvalidRequest(msg, provider=provider) from error
    if not data:
        msg = f"The {kind} at a remote URL is empty"
        raise InvalidRequest(msg, provider=provider)
    return data, declared


def _log_unavailable(url: str, kind: MediaKind, error: Exception) -> None:
    """Log a failed download by the URL's scheme, host and port, and path (``redact_url_credentials``).

    A refused download logs its status, because the status error's text repeats the whole URL.
    """
    logged_url = redact_url_credentials(url)
    if isinstance(error, httpx2.HTTPStatusError):
        logger.warning("remote_media_unavailable", kind=kind, url=logged_url, status_code=error.response.status_code)
    else:
        logger.warning("remote_media_unavailable", kind=kind, url=logged_url, error=str(error))
