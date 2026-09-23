"""Versioned, non-executable durable tool values and rich Agno artifacts."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agno.media import Audio, File, Image, Video
from agno.models.message import Message
from agno.tools.function import ToolResult

_MODELS = {model.__name__: model for model in (ToolResult, Image, Audio, Video, File, Message)}
_MAX_ENCODED_RESULT_BYTES = 64 * 1024 * 1024


@dataclass
class _EncodingBudget:
    """Track exact default-JSON UTF-8 bytes without materializing the document."""

    limit: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def charge(self, size: int) -> None:
        if size > self.remaining:
            msg = f"Durable tool result exceeds the {self.limit}-byte encoded JSON limit."
            raise ValueError(msg)
        self.used += size


@dataclass(frozen=True)
class _FileBytes:
    """Defer one local artifact read until its enclosing JSON overhead is charged."""

    path: Path


def _json_string_size(value: str) -> int:
    """Return json.dumps' default ensure-ascii byte size without a second string copy."""
    size = 2
    short_escapes = {'"', "\\", "\b", "\f", "\n", "\r", "\t"}
    for character in value:
        codepoint = ord(character)
        if character in short_escapes:
            size += 2
        elif codepoint <= 0x1F:
            size += 6
        elif codepoint < 0x7F:
            size += 1
        elif codepoint <= 0xFFFF:
            size += 6
        else:
            size += 12
    return size


def _charge_object(keys: tuple[str, ...], budget: _EncodingBudget) -> None:
    budget.charge(2 + max(0, len(keys) - 1) * 2 + len(keys) * 2)
    for key in keys:
        budget.charge(_json_string_size(key))


def _charge_array(length: int, budget: _EncodingBudget) -> None:
    budget.charge(2 + max(0, length - 1) * 2)


def _charge_tag(kind: str, budget: _EncodingBudget) -> None:
    _charge_object(("type", "value"), budget)
    budget.charge(_json_string_size(kind))


def _encoded_base64_json_size(raw_size: int) -> int:
    return 2 + 4 * ((raw_size + 2) // 3)


def _encode_bytes(value: bytes, budget: _EncodingBudget) -> dict[str, str]:
    _charge_tag("bytes", budget)
    budget.charge(_encoded_base64_json_size(len(value)))
    return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}


def _encode_file_bytes(value: _FileBytes, budget: _EncodingBudget) -> dict[str, str]:
    _charge_tag("bytes", budget)
    max_raw_bytes = 3 * (max(0, budget.remaining - 2) // 4)
    with value.path.open("rb") as source:
        raw = source.read(max_raw_bytes + 1)
    if len(raw) > max_raw_bytes:
        msg = f"Durable tool result exceeds the {budget.limit}-byte encoded JSON limit."
        raise ValueError(msg)
    budget.charge(_encoded_base64_json_size(len(raw)))
    return {"type": "bytes", "value": base64.b64encode(raw).decode("ascii")}


def _encode(value: Any, budget: _EncodingBudget) -> Any:  # noqa: ANN401, C901, PLR0911, PLR0912
    if isinstance(value, ToolResult):
        _charge_tag("ToolResult", budget)
        return {
            "type": "ToolResult",
            "value": _encode(
                {
                    "content": value.content,
                    "metadata": value.metadata,
                    "images": value.images,
                    "audios": value.audios,
                    "videos": value.videos,
                    "files": value.files,
                },
                budget,
            ),
        }
    if isinstance(value, (Image, Audio, Video, File)):
        _charge_tag(type(value).__name__, budget)
        fields = value.model_dump(mode="python")
        if value.filepath is not None:
            fields["filepath"] = None
            fields.pop("content", None)
            fields["content"] = _FileBytes(Path(value.filepath))
        return {"type": type(value).__name__, "value": _encode(fields, budget)}
    if isinstance(value, tuple(_MODELS.values())):
        _charge_tag(type(value).__name__, budget)
        return {"type": type(value).__name__, "value": _encode(value.model_dump(mode="python"), budget)}
    if isinstance(value, _FileBytes):
        return _encode_file_bytes(value, budget)
    if isinstance(value, bytes):
        return _encode_bytes(value, budget)
    if isinstance(value, Path):
        _charge_tag("path", budget)
        budget.charge(_json_string_size(str(value)))
        return {"type": "path", "value": str(value)}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            msg = "Tool result dictionaries require string keys"
            raise TypeError(msg)
        _charge_tag("dict", budget)
        _charge_object(tuple(value), budget)
        return {"type": "dict", "value": {key: _encode(item, budget) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        kind = "tuple" if isinstance(value, tuple) else "list"
        _charge_tag(kind, budget)
        _charge_array(len(value), budget)
        return {"type": kind, "value": [_encode(item, budget) for item in value]}
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str):
            budget.charge(_json_string_size(value))
        else:
            budget.charge(len(json.dumps(value, allow_nan=False)))
        return value
    msg = f"Unsupported durable tool result type: {type(value).__name__}"
    raise TypeError(msg)


def _decode(value: Any) -> Any:  # noqa: ANN401, PLR0911 - One explicit branch per supported wire tag.
    if not isinstance(value, dict):
        return value
    kind, data = value["type"], value["value"]
    if kind == "dict":
        return {key: _decode(item) for key, item in data.items()}
    if kind == "list":
        return [_decode(item) for item in data]
    if kind == "tuple":
        return tuple(_decode(item) for item in data)
    if kind == "bytes":
        return base64.b64decode(data, validate=True)
    if kind == "path":
        return Path(data)
    if kind in _MODELS:
        return _MODELS[kind].model_validate(_decode(data))
    msg = f"Unsupported durable tool result tag: {kind}"
    raise ValueError(msg)


def encode_tool_result(value: Any) -> dict[str, Any]:  # noqa: ANN401 - Public SDK value boundary.
    """Encode supported values within one 64 MiB encoded-JSON budget."""
    budget = _EncodingBudget(_MAX_ENCODED_RESULT_BYTES)
    _charge_object(("version", "value"), budget)
    budget.charge(1)
    return {"version": 1, "value": _encode(value, budget)}


def decode_tool_result(payload: dict[str, Any]) -> Any:  # noqa: ANN401 - Public SDK value boundary.
    """Restore an exact supported value from a persisted result envelope."""
    if payload["version"] != 1:
        msg = "Unsupported durable tool result version"
        raise ValueError(msg)
    return _decode(payload["value"])
