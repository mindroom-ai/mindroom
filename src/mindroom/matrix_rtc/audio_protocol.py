"""Length-prefixed JSON frames between MindRoom and its call audio process.

Each frame is a 4-byte big-endian payload length followed by a UTF-8 JSON
object with a string ``type``. Frame keys travel base64-encoded.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from typing import Any

_MAX_FRAME_BYTES = 1 << 20
_HEADER = struct.Struct(">I")


class AudioProtocolError(Exception):
    """A frame from the other process was malformed or cut off."""


def encode_frame(message: dict[str, Any]) -> bytes:
    """Return one message as a length-prefixed frame."""
    payload = json.dumps(message, separators=(",", ":")).encode()
    if len(payload) > _MAX_FRAME_BYTES:
        msg = f"frame of {len(payload)} bytes exceeds {_MAX_FRAME_BYTES}"
        raise AudioProtocolError(msg)
    return _HEADER.pack(len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Return the next message, or ``None`` when the stream ends between frames."""
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as error:
        if error.partial:
            msg = "stream ended inside a frame header"
            raise AudioProtocolError(msg) from error
        return None
    (length,) = _HEADER.unpack(header)
    if length > _MAX_FRAME_BYTES:
        msg = f"frame of {length} bytes exceeds {_MAX_FRAME_BYTES}"
        raise AudioProtocolError(msg)
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        msg = "stream ended inside a frame"
        raise AudioProtocolError(msg) from error
    try:
        message = json.loads(payload)
    except ValueError as error:
        msg = "frame is not valid JSON"
        raise AudioProtocolError(msg) from error
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        msg = "frame is not an object with a string type"
        raise AudioProtocolError(msg)
    return message


def encode_key(key: bytes) -> str:
    """Return frame-key bytes as base64 text."""
    return base64.b64encode(key).decode()


def decode_key(text: str) -> bytes:
    """Return frame-key bytes from base64 text."""
    return base64.b64decode(text, validate=True)
