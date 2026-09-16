"""Versioned, non-executable durable tool values and rich Agno artifacts."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from agno.media import Audio, File, Image, Video
from agno.models.message import Message
from agno.tools.function import ToolResult

_MODELS = {model.__name__: model for model in (ToolResult, Image, Audio, Video, File, Message)}


def _encode(value: Any) -> Any:  # noqa: ANN401, C901, PLR0911 - One explicit branch per supported wire tag.
    if isinstance(value, ToolResult):
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
            ),
        }
    if isinstance(value, (Image, Audio, Video, File)):
        fields = value.model_dump(mode="python")
        if value.filepath is not None:
            fields["content"] = Path(value.filepath).read_bytes()
            fields["filepath"] = None
        return {"type": type(value).__name__, "value": _encode(fields)}
    if isinstance(value, tuple(_MODELS.values())):
        return {"type": type(value).__name__, "value": _encode(value.model_dump(mode="python"))}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return {"type": "path", "value": str(value)}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            msg = "Tool result dictionaries require string keys"
            raise TypeError(msg)
        return {"type": "dict", "value": {key: _encode(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"type": "list", "value": [_encode(item) for item in value]}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    msg = f"Unsupported durable tool result type: {type(value).__name__}"
    raise TypeError(msg)


def _decode(value: Any) -> Any:  # noqa: ANN401 - Recursive heterogeneous SDK wire values.
    if not isinstance(value, dict):
        return value
    kind, data = value["type"], value["value"]
    if kind == "dict":
        return {key: _decode(item) for key, item in data.items()}
    if kind == "list":
        return [_decode(item) for item in data]
    if kind == "bytes":
        return base64.b64decode(data, validate=True)
    if kind == "path":
        return Path(data)
    if kind in _MODELS:
        return _MODELS[kind].model_validate(_decode(data))
    msg = f"Unsupported durable tool result tag: {kind}"
    raise ValueError(msg)


def encode_tool_result(value: Any) -> dict[str, Any]:  # noqa: ANN401 - Public SDK value boundary.
    """Encode JSON values and typed rich results without executable serialization."""
    payload = {"version": 1, "value": _encode(value)}
    json.dumps(payload, allow_nan=False)
    return payload


def decode_tool_result(payload: dict[str, Any]) -> Any:  # noqa: ANN401 - Public SDK value boundary.
    """Restore an exact supported value from a persisted result envelope."""
    if payload["version"] != 1:
        msg = "Unsupported durable tool result version"
        raise ValueError(msg)
    return _decode(payload["value"])
