"""Materialize tool media inside its worker before encoding inline results."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Any

import httpx
from agno.media import Audio, File, Image, Video
from agno.tools.function import ToolResult

from mindroom.server_fetch_url import ServerFetchHTTPTransport, ServerFetchUrlError
from mindroom.tool_system import media_transport
from mindroom.tool_system.worker_proxy_client import to_json_compatible

type _Media = Image | Audio | Video | File

if TYPE_CHECKING:
    from pathlib import Path


def _read_file(path: str | Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            msg = "Worker media must be a regular file within the byte limit."
            raise ValueError(msg)
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(limit + 1)
    finally:
        os.close(descriptor)


def _read_url(url: str, limit: int) -> tuple[bytes, str | None]:
    with (
        httpx.Client(
            transport=ServerFetchHTTPTransport(),
            trust_env=False,
            follow_redirects=True,
            max_redirects=5,
            timeout=30,
        ) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_bytes(chunk_size=64 * 1024):
            if len(content) + len(chunk) > limit:
                msg = "Worker media exceeds the byte limit."
                raise ValueError(msg)
            content.extend(chunk)
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip()
    return bytes(content), mime or None


def _materialize_media(media: _Media, limit: int) -> tuple[_Media, int]:
    if limit <= 0 or media.media_reference is not None or (isinstance(media, File) and media.external is not None):
        msg = "Unsupported worker media resource or exhausted byte limit."
        raise ValueError(msg)
    mime = media.mime_type
    try:
        if media.content is not None:
            content = media.content
        elif media.filepath is not None:
            content = _read_file(media.filepath, limit)
        elif media.url is not None:
            content, declared_mime = _read_url(media.url, limit)
            family = media_transport.MEDIA_MIME_FAMILIES.get(type(media))
            if mime is None and declared_mime and (family is None or declared_mime.startswith(family)):
                mime = declared_mime
        else:
            msg = "Worker media has no content source."
            raise ValueError(msg)
    except (OSError, httpx.HTTPError, ServerFetchUrlError):
        msg = "Unable to read worker media resource."
        raise ValueError(msg) from None
    if isinstance(media, File) and isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, bytes) or not content or len(content) > limit:
        msg = "Worker media must contain bytes within the byte limit."
        raise ValueError(msg)
    return media.model_copy(update={"content": content, "url": None, "filepath": None, "mime_type": mime}), len(content)


def serialize_worker_tool_result(result: object) -> object:
    """Resolve bounded media in the worker; ordinary results keep their JSON form."""
    if not isinstance(result, ToolResult):
        return to_json_compatible(result)
    sources = {
        "images": result.images or [],
        "audios": result.audios or [],
        "videos": result.videos or [],
        "files": result.files or [],
    }
    if sum(map(len, sources.values())) > media_transport.MAX_MEDIA_COUNT:
        msg = "Worker media exceeds the item limit."
        raise ValueError(msg)
    total = 0
    prepared: dict[str, Any] = {}
    for field, items in sources.items():
        prepared[field] = []
        for media in items:
            limit = min(media_transport.MAX_MEDIA_BYTES, media_transport.MAX_TOTAL_MEDIA_BYTES - total)
            item, size = _materialize_media(media, limit)
            total += size
            prepared[field].append(item)
    return media_transport.encode_media_result(result.model_copy(update=prepared))
