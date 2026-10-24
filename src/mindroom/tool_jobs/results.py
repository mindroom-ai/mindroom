"""The typed job result payload and its versioned, non-executable encoding of tool values and their media."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import stat
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.llm.media import Audio, File, Image, Video
from mindroom.tool_jobs.legacy_results import drop_legacy_payload_fields
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from mindroom.tool_jobs.runtime import BackgroundJob, EncodedResultPayload, ToolJobRuntime

_MEDIA = {kind.__name__: kind for kind in (Image, Audio, Video, File)}
# The media of a tool value under the field names saved payloads use.
_TOOL_VALUE_MEDIA = ("images", "audios", "videos", "files")
_MAX_ENCODED_RESULT_BYTES = 64 * 1024 * 1024
_SUMMARY_TRUNCATED_NOTICE = "[Truncated: only the start of this runtime message was saved.]"


@dataclass(frozen=True)
class ToolResultPayload:
    """Everything a consumer reads back from one finished job, stored once."""

    value: Any
    state_delta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _size_error() -> ValueError:
    return ValueError(f"Durable tool result exceeds the {_MAX_ENCODED_RESULT_BYTES}-byte encoded JSON limit.")


@dataclass
class _FileAllowance:
    """Raw artifact bytes one encoding may still read, so many files cannot exhaust memory before the size check."""

    remaining: int


def _file_bytes(path: Path, allowance: _FileAllowance) -> bytes:
    """Read a local artifact within the remaining allowance, even if the file grows meanwhile.

    Worker code can replace a workspace file, so only a regular file whose last component is no link is read, and a
    FIFO cannot stall the read.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            msg = "A tool result's file must be a regular file."
            raise ValueError(msg)
        if info.st_size > allowance.remaining:
            raise _size_error()
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(allowance.remaining + 1)
    finally:
        os.close(descriptor)
    if len(raw) > allowance.remaining:
        raise _size_error()
    allowance.remaining -= len(raw)
    return raw


def _encode(value: Any, allowance: _FileAllowance) -> Any:  # noqa: ANN401, C901, PLR0911 - One explicit branch per supported wire tag.
    if isinstance(value, ToolValue):
        media = (value.images, value.audios, value.videos, value.files)
        fields: dict[str, Any] = {"content": value.content}
        fields.update({name: list(items) for name, items in zip(_TOOL_VALUE_MEDIA, media, strict=True)})
        return {"type": "ToolResult", "value": _encode(fields, allowance)}
    if isinstance(value, (Image, Audio, Video, File)):
        fields = asdict(value)
        if value.filepath is not None:
            fields["filepath"] = None
            # Bytes the tool returned stand; its path is read only when they are missing.
            if value.content is None:
                fields["content"] = _file_bytes(Path(value.filepath), allowance)
        return {"type": type(value).__name__, "value": _encode(fields, allowance)}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return {"type": "path", "value": str(value)}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            # Keys such as 1 and "1" would collide as text, so the whole mapping keeps the text the model reads inline.
            return str(value)
        return {"type": "dict", "value": {key: _encode(item, allowance) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        kind = "tuple" if isinstance(value, tuple) else "list"
        return {"type": kind, "value": [_encode(item, allowance) for item in value]}
    if value is None or isinstance(value, (str, int, bool)) or (isinstance(value, float) and math.isfinite(value)):
        return value
    # Any other value, such as a datetime or a driver's own type, keeps the text the model reads for it inline.
    return str(value)


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
    if kind == "ToolResult":
        fields = _decode(data)
        media = {name: tuple(fields.get(name) or ()) for name in _TOOL_VALUE_MEDIA}
        return ToolValue(content=fields["content"], **media)
    if kind in _MEDIA:
        return _media(_MEDIA[kind], _decode(data))
    msg = f"Unsupported durable tool result tag: {kind}"
    raise ValueError(msg)


def _media(kind: type[Image | Audio | Video | File], saved: dict[str, Any]) -> Image | Audio | Video | File:
    """Rebuild one medium from the fields its type models, ignoring the rest, as every ``mindroom.llm`` codec does."""
    return kind(**{item.name: saved[item.name] for item in dataclass_fields(kind) if saved.get(item.name) is not None})


def encode_tool_result(value: Any) -> dict[str, Any]:  # noqa: ANN401 - Public SDK value boundary.
    """Encode one supported value in a versioned envelope within the 64 MiB encoded-JSON limit."""
    # Base64 turns every 3 raw artifact bytes into 4 encoded bytes.
    envelope = {"version": 1, "value": _encode(value, _FileAllowance(3 * (_MAX_ENCODED_RESULT_BYTES // 4)))}
    # Default JSON escapes every non-ASCII character, so its length is the saved byte count.
    if len(json.dumps(envelope, allow_nan=False)) > _MAX_ENCODED_RESULT_BYTES:
        raise _size_error()
    return envelope


def encode_result_payload(payload: ToolResultPayload) -> EncodedResultPayload:
    """Encode a whole job result in one envelope, so one limit covers every field."""
    return encode_tool_result(vars(payload))


def _decode_result_payload(envelope: EncodedResultPayload) -> ToolResultPayload:
    if envelope["version"] != 1:
        msg = "Unsupported durable tool result version"
        raise ValueError(msg)
    encoded = envelope["value"]
    return ToolResultPayload(**_decode({**encoded, "value": drop_legacy_payload_fields(encoded["value"])}))


async def read_result_payload(runtime: ToolJobRuntime, job: BackgroundJob) -> ToolResultPayload:
    """Read a ready job's full result from its saved payload; an outcome the runtime authored itself has only its summary."""
    if job.has_result_payload:
        return await asyncio.to_thread(_decode_result_payload, await runtime.read_payload(job))
    if job.summary_truncated:
        return ToolResultPayload(value=f"{job.result}\n{_SUMMARY_TRUNCATED_NOTICE}")
    return ToolResultPayload(value=job.result)
