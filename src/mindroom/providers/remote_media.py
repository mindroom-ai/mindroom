"""Download remote media for providers that take media only as inline bytes.

Tools such as image generators return media as URLs; Gemini and Ollama need the bytes in the request.
A URL that cannot be read raises ``InvalidRequest``, so the media fallback replaces the media with its note
instead of the model answering about media it never saw.
"""

from __future__ import annotations

import httpx

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.llm.errors import InvalidRequest
from mindroom.llm.media import canonical_mime_type
from mindroom.logging_config import get_logger

logger = get_logger(__name__)

_DOWNLOAD_TIMEOUT_SECONDS = 30.0


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT_SECONDS)


async def download_media(url: str, *, kind: str, limit_bytes: int, provider: str) -> tuple[bytes, str | None]:
    """Return the bytes at ``url`` and the MIME type the server declared, reading at most ``limit_bytes``."""
    try:
        async with (
            _http_client() as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            data = await collect_bounded_bytes(response.aiter_bytes(), max_bytes=limit_bytes)
            declared = canonical_mime_type(response.headers.get("content-type"))
    except (httpx.HTTPError, httpx.InvalidURL, ByteLimitExceededError) as error:
        logger.warning("remote_media_unavailable", kind=kind, url=url, error=str(error))
        msg = f"The {kind} at a remote URL could not be downloaded for the model"
        raise InvalidRequest(msg, provider=provider) from error
    if not data:
        msg = f"The {kind} at a remote URL is empty"
        raise InvalidRequest(msg, provider=provider)
    return data, declared


__all__ = ["download_media"]
