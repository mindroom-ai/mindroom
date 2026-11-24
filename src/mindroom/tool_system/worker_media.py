"""Materialize tool media inside its worker before encoding inline results."""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

from mindroom.llm.media import Audio, File, Image, Video, canonical_mime_type, named_by_resource, url_file_name
from mindroom.public_fetch import PublicFetchError, fetch_public_url_sync
from mindroom.tool_system import media_transport
from mindroom.tool_system.agno_toolkits import read_tool_value
from mindroom.tool_system.worker_proxy_client import to_json_compatible

type _Media = Image | Audio | Video | File


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


def _resource_name(media: _Media) -> str:
    if media.filepath is not None:
        return Path(media.filepath).name
    return url_file_name(media.url or "")


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
            # Proxy-only workers reach the internet through their egress proxy, which owns its hostname grants.
            fetched = fetch_public_url_sync(media.url, max_bytes=limit, use_env_proxy=True)
            content, declared_mime = fetched.content, canonical_mime_type(fetched.content_type)
        else:
            msg = "Worker media has no content source."
            raise ValueError(msg)
    except OSError:
        msg = "Unable to read worker media resource."
        raise ValueError(msg) from None
    except PublicFetchError as error:
        msg = f"Unable to read worker media resource: {error}"
        raise ValueError(msg) from None
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not content or len(content) > limit:
        msg = "Worker media must contain bytes within the byte limit."
        raise ValueError(msg)
    # The resource's name and declared type keep identifying the media once its location is gone.
    named = named_by_resource(media, _resource_name(media), declared_mime)
    return replace(named, content=content, url=None, filepath=None), len(content)


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
