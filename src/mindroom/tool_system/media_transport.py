"""Bounded, inline-only JSON transport for typed tool media results."""

from __future__ import annotations

import base64
import binascii
import json
import math
from dataclasses import dataclass
from typing import Any, cast

from agno.media import Audio, File, Image, Video
from agno.tools.function import ToolResult
from pydantic import ValidationError

MAX_MEDIA_COUNT = 8
MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_TOTAL_MEDIA_BYTES = 20 * 1024 * 1024
_MAX_CONTENT_CHARS = 4 * 1024 * 1024
_MAX_METADATA_CHARS = 64 * 1024
_MAX_ENCODED_BYTES = 4 * ((MAX_MEDIA_BYTES + 2) // 3)
_MAX_MEDIA_ID_CHARS = 256
_KEY = "mindroom_tool_result"
_MEDIA_MODELS = {"images": Image, "audios": Audio, "videos": Video, "files": File}
_RESOURCE_FIELDS = {"content", "url", "filepath", "media_reference", "external"}
type _Media = Image | Audio | Video | File
MEDIA_MIME_FAMILIES: dict[type[_Media], str] = {Image: "image/", Audio: "audio/", Video: "video/"}


@dataclass(frozen=True, slots=True)
class _EncodedMedia:
    field: str
    model: type[_Media]
    attributes: dict[str, Any]
    data: str


def _invalid() -> ValueError:
    return ValueError("Invalid worker tool result envelope.")


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    return isinstance(value, dict) and all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())


def _json_size(value: object, limit: int) -> int:
    try:
        if not _is_json_value(value):
            raise _invalid()
        size = len(json.dumps(value, allow_nan=False))
    except (TypeError, RecursionError) as exc:
        raise _invalid() from exc
    if size > limit:
        raise _invalid()
    return size


def is_media_result_envelope(payload: object) -> bool:
    """Return whether a payload claims the reserved tool-result key."""
    return isinstance(payload, dict) and _KEY in payload


def _encode_media(media: _Media) -> dict[str, Any]:
    if (
        not isinstance(media.content, bytes)
        or media.url is not None
        or media.filepath is not None
        or media.media_reference is not None
        or (isinstance(media, File) and media.external is not None)
        or not media.content
        or len(media.content) > MAX_MEDIA_BYTES
    ):
        raise _invalid()
    entry = media.model_dump(exclude=_RESOURCE_FIELDS, exclude_none=True)
    _json_size(entry, _MAX_METADATA_CHARS)
    entry["data_base64"] = base64.b64encode(media.content).decode("ascii")
    return entry


def encode_media_result(result: object) -> dict[str, object]:
    """Encode JSON data or a bounded typed result without resolving resources."""
    if isinstance(result, ToolResult):
        media = (result.images or [], result.audios or [], result.videos or [], result.files or [])
        if sum(map(len, media)) > MAX_MEDIA_COUNT:
            raise _invalid()
        total = sum(len(item.content) for items in media for item in items if isinstance(item.content, bytes))
        if total > MAX_TOTAL_MEDIA_BYTES:
            raise _invalid()
        value: dict[str, object] = {"version": 1, "kind": "tool_result", "content": result.content}
        for field, items in zip(_MEDIA_MODELS, media, strict=True):
            value[field] = [_encode_media(item) for item in items]
        if result.metadata is not None:
            value["metadata"] = result.metadata
    else:
        _json_size(result, _MAX_CONTENT_CHARS)
        value = {"version": 1, "kind": "json", "value": result}
    payload: dict[str, object] = {_KEY: value}
    decode_media_result(payload)
    return payload


def _validate_media_entry(entry: object, model: type[_Media]) -> tuple[dict[str, Any], str]:
    if not isinstance(entry, dict) or "data_base64" not in entry:
        raise _invalid()
    entry = cast("dict[str, Any]", entry)
    allowed = set(model.model_fields) - _RESOURCE_FIELDS
    if not set(entry) <= allowed | {"data_base64"}:
        raise _invalid()
    encoded = entry["data_base64"]
    if not isinstance(encoded, str) or not encoded or len(encoded) > _MAX_ENCODED_BYTES:
        raise _invalid()
    attributes = {key: value for key, value in entry.items() if key != "data_base64"}
    identifier = attributes.get("id")
    if identifier is not None and (
        not isinstance(identifier, str) or not identifier or len(identifier) > _MAX_MEDIA_ID_CHARS
    ):
        raise _invalid()
    mime = attributes.get("mime_type")
    if mime is not None:
        family = MEDIA_MIME_FAMILIES.get(model)
        if (
            not isinstance(mime, str)
            or "/" not in mime
            or any(character.isspace() for character in mime)
            or (family is not None and not mime.startswith(family))
        ):
            raise _invalid()
    return attributes, encoded


def decode_media_result(payload: object) -> object:
    """Decode inline bytes only; never restore resource references or perform IO."""
    if not isinstance(payload, dict) or set(payload) != {_KEY}:
        raise _invalid()
    value = cast("dict[str, object]", payload)[_KEY]
    if not isinstance(value, dict):
        raise _invalid()
    value = cast("dict[str, Any]", value)
    if type(value.get("version")) is not int or value["version"] != 1:
        raise _invalid()
    if value.get("kind") == "json":
        if set(value) != {"version", "kind", "value"}:
            raise _invalid()
        _json_size(value["value"], _MAX_CONTENT_CHARS)
        return value["value"]
    required = {"version", "kind", "content", *_MEDIA_MODELS}
    if value.get("kind") != "tool_result" or set(value) not in (required, required | {"metadata"}):
        raise _invalid()
    return _decode_tool_result(value)


def _decode_tool_result(value: dict[str, Any]) -> ToolResult:
    content = value["content"]
    metadata = value.get("metadata")
    if not isinstance(content, str) or len(content) > _MAX_CONTENT_CHARS:
        raise _invalid()
    if metadata is not None and not isinstance(metadata, dict):
        raise _invalid()
    metadata_size = _json_size(metadata, _MAX_METADATA_CHARS)
    pending: list[_EncodedMedia] = []
    for field, model in _MEDIA_MODELS.items():
        entries = value[field]
        if not isinstance(entries, list) or len(pending) + len(entries) > MAX_MEDIA_COUNT:
            raise _invalid()
        for entry in entries:
            attributes, encoded = _validate_media_entry(entry, model)
            metadata_size += _json_size(attributes, _MAX_METADATA_CHARS)
            pending.append(_EncodedMedia(field, model, attributes, encoded))
    max_encoded = 4 * ((MAX_TOTAL_MEDIA_BYTES + 2) // 3) + 4 * (MAX_MEDIA_COUNT - 1)
    if metadata_size > _MAX_METADATA_CHARS or sum(len(item.data) for item in pending) > max_encoded:
        raise _invalid()
    decoded: dict[str, Any] = {field: [] for field in _MEDIA_MODELS}
    total = 0
    for item in pending:
        media, data = _decode_media_entry(item)
        total += len(data)
        if total > MAX_TOTAL_MEDIA_BYTES:
            raise _invalid()
        decoded[item.field].append(media)
    return ToolResult(content=content, metadata=metadata, **{field: items or None for field, items in decoded.items()})


def _decode_media_entry(item: _EncodedMedia) -> tuple[_Media, bytes]:
    try:
        data = base64.b64decode(item.data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _invalid() from exc
    if not data or len(data) > MAX_MEDIA_BYTES or base64.b64encode(data).decode("ascii") != item.data:
        raise _invalid()
    try:
        media = item.model.model_validate({**item.attributes, "content": data}, strict=True)
    except ValidationError as exc:
        raise _invalid() from exc
    return media, data
