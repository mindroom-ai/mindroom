"""Materialize tool media inside its worker before encoding inline results."""

from __future__ import annotations

import mimetypes
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from httpx._utils import get_environment_proxies

from mindroom.llm.media import Audio, File, Image, Video, canonical_mime_type, named_by_resource
from mindroom.server_fetch_url import ServerFetchHTTPTransport, ServerFetchUrlError, validate_server_fetch_url
from mindroom.tool_system import media_transport
from mindroom.tool_system.agno_toolkits import read_tool_value
from mindroom.tool_system.worker_proxy_client import to_json_compatible

type _Media = Image | Audio | Video | File

_AUDIO_FORMAT_BY_MIME_TYPE = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
}


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
    with httpx.Client(
        transport=ServerFetchHTTPTransport(),
        mounts=mounts,
        trust_env=False,
        headers={"Accept-Encoding": "identity"},
        follow_redirects=False,
        max_redirects=5,
        timeout=30,
    ) as client:
        request = client.build_request("GET", url)
        for _ in range(client.max_redirects + 1):
            response = client.send(request, stream=True)
            try:
                if response.next_request is not None:
                    # HTTPX automatic redirects read the whole intermediate body.
                    request = response.next_request
                    continue
                response.raise_for_status()
                encoding = response.headers.get("content-encoding", "")
                if any(value.strip().lower() not in {"", "identity"} for value in encoding.split(",")):
                    msg = "Worker media requires identity content encoding."
                    raise ValueError(msg)
                content = bytearray()
                for chunk in response.iter_raw(chunk_size=64 * 1024):
                    if len(content) + len(chunk) > limit:
                        msg = "Worker media exceeds the byte limit."
                        raise ValueError(msg)
                    content.extend(chunk)
                return bytes(content), canonical_mime_type(response.headers.get("content-type"))
            finally:
                response.close()
        msg = "Exceeded maximum allowed redirects."
        raise httpx.TooManyRedirects(msg, request=request)


def _resource_name(media: _Media) -> str:
    if media.filepath is not None:
        return Path(media.filepath).name
    return Path(unquote(urlsplit(media.url or "").path)).name


def _inline_metadata(media: _Media, declared_mime: str | None) -> dict[str, Any]:
    """Retain provider-relevant resource identity before dropping its location."""
    name = _resource_name(media)
    if isinstance(media, File):
        named = named_by_resource(media, name, declared_mime)
        return {"filename": named.filename, "mime_type": named.mime_type}
    updates: dict[str, Any] = {}
    if media.mime_type is None and media.format is None:
        family = media_transport.MEDIA_MIME_FAMILIES[type(media)]
        inferred_mime = mimetypes.guess_type(name)[0] if isinstance(media, Image) else None
        candidates = (declared_mime, inferred_mime)
        updates["mime_type"] = next((value for value in candidates if value and value.startswith(family)), None)
    if isinstance(media, (Audio, Video)) and media.format is None and name:
        updates["format"] = Path(name).suffix.removeprefix(".").lower() or None
    if isinstance(media, Audio) and media.format is None:
        suffix_mime = mimetypes.guess_type(name)[0] or ""
        if updates.get("format") not in {"mp3", "wav", "ogg", "webm"} and not suffix_mime.startswith("audio/"):
            mime = media.mime_type or updates.get("mime_type")
            if audio_format := _AUDIO_FORMAT_BY_MIME_TYPE.get(mime or ""):
                updates["format"] = audio_format
    return updates


def _materialize_media(media: _Media, limit: int) -> tuple[_Media, int]:
    if limit <= 0:
        msg = "Worker media exhausted the byte limit."
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
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not content or len(content) > limit:
        msg = "Worker media must contain bytes within the byte limit."
        raise ValueError(msg)
    updates = {"content": content, "url": None, "filepath": None, **_inline_metadata(media, declared_mime)}
    return replace(media, **updates), len(content)


def serialize_worker_tool_result(result: object) -> object:
    """Resolve bounded media in the worker; ordinary results keep their JSON form."""
    value = read_tool_value(result)
    if value is None:
        result = to_json_compatible(result)
        if media_transport.is_media_result_envelope(result):
            return media_transport.encode_media_result(result)
        return result
    if sum(1 for _ in value.media()) > media_transport.MAX_MEDIA_COUNT:
        msg = "Worker media exceeds the item limit."
        raise ValueError(msg)
    total = 0
    prepared: dict[str, list[Any]] = {"images": [], "audios": [], "videos": [], "files": []}
    for field, items in zip(prepared, (value.images, value.audios, value.videos, value.files), strict=True):
        for media in items:
            limit = min(media_transport.MAX_MEDIA_BYTES, media_transport.MAX_TOTAL_MEDIA_BYTES - total)
            item, size = _materialize_media(media, limit)
            total += size
            prepared[field].append(item)
    return media_transport.encode_media_result(
        replace(value, **{field: tuple(items) for field, items in prepared.items()}),
    )
