"""Bounded JSON transport for tool results containing inline image bytes."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, cast

from agno.media import Image
from agno.tools.function import ToolResult

_KEY = "mindroom_browser_mcp_result"
_MAX_CONTENT_CHARS = 4 * 1024 * 1024
_MAX_IMAGES = 8
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_BYTES = 20 * 1024 * 1024
_MAX_ENCODED_BYTES = 4 * ((_MAX_IMAGE_BYTES + 2) // 3)
_MAX_IMAGE_ID_CHARS = 256
_MIME_TYPES = frozenset({"image/png", "image/jpeg"})


def _invalid() -> ValueError:
    # Keep the established message while the browser-specific wire key remains
    # compatible with already-running workers.
    return ValueError("Invalid browser MCP result envelope.")


def is_media_result_envelope(payload: object) -> bool:
    """Return whether a payload claims the reserved media transport key."""
    return isinstance(payload, dict) and _KEY in payload


def encode_media_result(result: object) -> dict[str, object]:  # noqa: C901 - strict protocol validation
    """Encode JSON values or one bounded image-bearing ``ToolResult``."""
    if isinstance(result, ToolResult):
        if result.audios or result.videos or result.files or result.metadata:
            raise _invalid()
        source_images = result.images or []
        if (
            len(source_images) > _MAX_IMAGES
            or not isinstance(result.content, str)
            or len(result.content) > _MAX_CONTENT_CHARS
        ):
            raise _invalid()
        total_bytes = 0
        for image in source_images:
            if not isinstance(image.content, bytes) or image.url or image.filepath:
                raise _invalid()
            if image.id is not None and (
                not isinstance(image.id, str) or not image.id or len(image.id) > _MAX_IMAGE_ID_CHARS
            ):
                raise _invalid()
            total_bytes += len(image.content)
            if not image.content or len(image.content) > _MAX_IMAGE_BYTES or total_bytes > _MAX_TOTAL_BYTES:
                raise _invalid()
        images = []
        for image in source_images:
            entry = {
                "mime_type": image.mime_type,
                "data_base64": base64.b64encode(cast("bytes", image.content)).decode("ascii"),
            }
            if image.id is not None:
                entry["id"] = image.id
            images.append(entry)
        value = {"version": 1, "kind": "tool_result", "content": result.content, "images": images}
    else:
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise _invalid() from exc
        value = {"version": 1, "kind": "json", "value": result}
    payload: dict[str, object] = {_KEY: value}
    decode_media_result(payload)
    return payload


def decode_media_result(payload: object) -> object:  # noqa: C901, PLR0912 - strict protocol validation
    """Decode one trusted result without reading paths or fetching URLs."""
    if not isinstance(payload, dict) or set(payload) != {_KEY}:
        raise _invalid()
    value = cast("dict[str, Any]", payload)[_KEY]
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise _invalid()
    if value.get("kind") == "json":
        if set(value) != {"version", "kind", "value"}:
            raise _invalid()
        return value["value"]
    if value.get("kind") != "tool_result" or set(value) != {"version", "kind", "content", "images"}:
        raise _invalid()
    content, entries = value["content"], value["images"]
    if not isinstance(content, str) or len(content) > _MAX_CONTENT_CHARS:
        raise _invalid()
    if not isinstance(entries, list) or len(entries) > _MAX_IMAGES:
        raise _invalid()
    max_total_encoded = 4 * ((_MAX_TOTAL_BYTES + 2) // 3) + 4 * (_MAX_IMAGES - 1)
    total_encoded = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) not in (
            {"mime_type", "data_base64"},
            {"mime_type", "data_base64", "id"},
        ):
            raise _invalid()
        entry = cast("dict[str, Any]", entry)
        mime, encoded = entry["mime_type"], entry["data_base64"]
        if not isinstance(mime, str) or mime not in _MIME_TYPES or not isinstance(encoded, str):
            raise _invalid()
        image_id = entry.get("id")
        if image_id is not None and (
            not isinstance(image_id, str) or not image_id or len(image_id) > _MAX_IMAGE_ID_CHARS
        ):
            raise _invalid()
        total_encoded += len(encoded)
        if not encoded or len(encoded) > _MAX_ENCODED_BYTES or total_encoded > max_total_encoded:
            raise _invalid()
    images = []
    total = 0
    for entry in entries:
        try:
            data = base64.b64decode(entry["data_base64"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise _invalid() from exc
        total += len(data)
        if (
            not data
            or len(data) > _MAX_IMAGE_BYTES
            or total > _MAX_TOTAL_BYTES
            or base64.b64encode(data).decode("ascii") != entry["data_base64"]
        ):
            raise _invalid()
        images.append(Image(id=entry.get("id"), content=data, mime_type=entry["mime_type"]))
    return ToolResult(content=content, images=images or None)
