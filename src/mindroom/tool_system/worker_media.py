"""Materialize tool media inside its worker before encoding inline results."""

from __future__ import annotations

import mimetypes
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from agno.media import Audio, File, Image, Video
from agno.tools.function import ToolResult
from httpx._utils import get_environment_proxies

from mindroom.server_fetch_url import ServerFetchHTTPTransport, ServerFetchUrlError, validate_server_fetch_url
from mindroom.tool_system import media_transport
from mindroom.tool_system.worker_proxy_client import to_json_compatible

type _Media = Image | Audio | Video | File


class _WorkerMediaProxyTransport(httpx.HTTPTransport):
    """Validate media destinations before forwarding through configured egress."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Check every origin and redirect before sending it to the worker proxy."""
        # The configured proxy owns its final connection and hostname grant policy.
        validate_server_fetch_url(str(request.url))
        return super().handle_request(request)


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
    # HTTPX disables environment proxy discovery with an explicit transport.
    # Reuse its routing map so scheme selection and NO_PROXY keep their semantics.
    mounts = {
        pattern: None if proxy is None else _WorkerMediaProxyTransport(proxy=proxy)
        for pattern, proxy in get_environment_proxies().items()
    }
    with (
        httpx.Client(
            transport=ServerFetchHTTPTransport(),
            mounts=mounts,
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
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return bytes(content), mime or None


def _resource_name(media: _Media) -> str:
    if media.filepath is not None:
        return Path(media.filepath).name
    return Path(unquote(urlsplit(media.url or "").path)).name


def _inline_metadata(media: _Media, declared_mime: str | None) -> dict[str, Any]:
    """Retain provider-relevant resource identity before dropping its location."""
    name = _resource_name(media)
    if isinstance(media, File):
        filename = media.filename or media.name or name or None
        candidates = (declared_mime, mimetypes.guess_type(filename or "")[0], mimetypes.guess_type(name)[0])
        mime = media.mime_type or next((value for value in candidates if value in File.valid_mime_types()), None)
        return {"filename": filename, "mime_type": mime}
    updates: dict[str, Any] = {}
    if media.mime_type is None and media.format is None:
        family = media_transport.MEDIA_MIME_FAMILIES[type(media)]
        inferred_mime = mimetypes.guess_type(name)[0] if isinstance(media, Image) else None
        candidates = (declared_mime, inferred_mime)
        updates["mime_type"] = next((value for value in candidates if value and value.startswith(family)), None)
    if isinstance(media, (Audio, Video)) and media.format is None and name:
        updates["format"] = Path(name).suffix.removeprefix(".").lower() or None
    return updates


def _materialize_media(media: _Media, limit: int) -> tuple[_Media, int]:
    if limit <= 0 or media.media_reference is not None or (isinstance(media, File) and media.external is not None):
        msg = "Unsupported worker media resource or exhausted byte limit."
        raise ValueError(msg)
    declared_mime = None
    try:
        if media.content is not None:
            content = media.content
        elif media.filepath is not None:
            content = _read_file(media.filepath, limit)
        elif media.url is not None:
            content, declared_mime = _read_url(media.url, limit)
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
    updates = {"content": content, "url": None, "filepath": None, **_inline_metadata(media, declared_mime)}
    return media.model_copy(update=updates), len(content)


def serialize_worker_tool_result(result: object) -> object:
    """Resolve bounded media in the worker; ordinary results keep their JSON form."""
    if not isinstance(result, ToolResult):
        result = to_json_compatible(result)
        if media_transport.is_media_result_envelope(result):
            return media_transport.encode_media_result(result)
        return result
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
