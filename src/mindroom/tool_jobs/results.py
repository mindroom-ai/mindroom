"""The typed job result payload and its versioned, non-executable encoding of tool values and rich Agno artifacts."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.media import Audio, File, Image, Video
from agno.models.message import Message
from agno.tools.function import ToolResult

if TYPE_CHECKING:
    from mindroom.tool_jobs.runtime import BackgroundJob

_MODELS = {model.__name__: model for model in (ToolResult, Image, Audio, Video, File, Message)}
_MAX_ENCODED_RESULT_BYTES = 64 * 1024 * 1024
# One drained stream item: the length of its text within the value's text, and its SDK event without that text.
type ReplayItem = tuple[int, dict[str, Any] | None]


@dataclass(frozen=True)
class ToolResultPayload:
    """Everything a consumer reads back from one finished job, stored once."""

    value: Any
    state_delta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    elapsed: float = 0.0
    replay: tuple[ReplayItem, ...] = ()
    control: dict[str, Any] | None = None


def _size_error() -> ValueError:
    return ValueError(f"Durable tool result exceeds the {_MAX_ENCODED_RESULT_BYTES}-byte encoded JSON limit.")


def _file_bytes(path: Path) -> bytes:
    """Read a local artifact whose base64 form can still fit, even if the file grows meanwhile."""
    limit = 3 * (_MAX_ENCODED_RESULT_BYTES // 4)
    if path.stat().st_size > limit:
        raise _size_error()
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise _size_error()
    return raw


def _encode(value: Any) -> Any:  # noqa: ANN401, C901, PLR0911 - One explicit branch per supported wire tag.
    if isinstance(value, ToolResult):
        fields = {
            "content": value.content,
            "metadata": value.metadata,
            "images": value.images,
            "audios": value.audios,
            "videos": value.videos,
            "files": value.files,
        }
        return {"type": "ToolResult", "value": _encode(fields)}
    if isinstance(value, (Image, Audio, Video, File)):
        fields = value.model_dump(mode="python")
        if value.filepath is not None:
            fields["filepath"] = None
            fields["content"] = _file_bytes(Path(value.filepath))
        return {"type": type(value).__name__, "value": _encode(fields)}
    if isinstance(value, Message):
        return {"type": "Message", "value": _encode(value.model_dump(mode="python"))}
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
        kind = "tuple" if isinstance(value, tuple) else "list"
        return {"type": kind, "value": [_encode(item) for item in value]}
    if value is None or isinstance(value, (str, int, float, bool)):
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
    """Encode one supported value in a versioned envelope within the 64 MiB encoded-JSON limit."""
    envelope = {"version": 1, "value": _encode(value)}
    # Default JSON escapes every non-ASCII character, so its length is the saved byte count.
    if len(json.dumps(envelope, allow_nan=False)) > _MAX_ENCODED_RESULT_BYTES:
        raise _size_error()
    return envelope


def encode_result_payload(payload: ToolResultPayload) -> dict[str, Any]:
    """Encode a whole job result in one envelope, so one limit covers every field."""
    return encode_tool_result(vars(payload))


def _decode_result_payload(envelope: dict[str, Any]) -> ToolResultPayload:
    if envelope["version"] != 1:
        msg = "Unsupported durable tool result version"
        raise ValueError(msg)
    return ToolResultPayload(**_decode(envelope["value"]))


def read_result_payload(job: BackgroundJob) -> ToolResultPayload:
    """Read the full result of a snapshot that includes it; an outcome the runtime authored itself has only its summary."""
    if job.result_payload is None:
        return ToolResultPayload(value=job.result)
    return _decode_result_payload(job.result_payload)
