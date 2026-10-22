# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Inline media content parts for Chat Completions and Responses requests.

Attachments are filepath-backed, so parts are read and base64 encoded here; remote file and audio URLs are
not fetched (MindRoom never builds them), and image URLs pass through for the provider to fetch.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.llm.media import Audio, File, Image

__all__ = [
    "chat_audio_part",
    "chat_file_part",
    "chat_image_part",
    "responses_file_part",
    "responses_image_part",
]

logger = get_logger(__name__)

# Python's MIME database lacks or misnames these supported formats; host /etc/mime.types must not decide.
_RESPONSES_FILE_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".sh": "text/x-sh",
}


# Image formats by their header bytes: (offset, signature, format).
_IMAGE_SIGNATURES = (
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\xff\xd8\xff", "jpeg"),
    (4, b"ftyp", "heic"),
)


def _image_type(data: bytes) -> str | None:
    """Return the image format named by the file header's magic bytes."""
    if len(data) < 12:
        return None
    if data[0:4] == b"GIF8" and data[5:6] == b"a":
        return "gif"
    if data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((name for offset, signature, name in _IMAGE_SIGNATURES if data[offset:].startswith(signature)), None)


def _image_mime_type(image: Image, data: bytes) -> str:
    """Resolve an image MIME type: explicit type, then format, then path extension, then magic bytes."""
    if image.mime_type:
        return image.mime_type
    if image.format:
        return f"image/{image.format.lower()}"
    if image.filepath and (guessed := mimetypes.guess_type(image.filepath)[0]):
        return guessed
    detected = _image_type(data)
    return f"image/{detected}" if detected else "image/jpeg"


def _file_bytes(path: str) -> bytes | None:
    file_path = Path(path)
    if not file_path.is_file():
        logger.warning("Model input file not found", path=path)
        return None
    return file_path.read_bytes()


def _image_url(image: Image) -> str | None:
    if image.url is not None:
        if image.url.startswith(("data:image", "http://", "https://")):
            return image.url
        logger.warning("Image URL must start with 'data:image' or 'http(s)://'", image_id=image.id)
        return None
    data = _file_bytes(image.filepath) if image.filepath is not None else image.content
    if data is None:
        return None
    return f"data:{_image_mime_type(image, data)};base64,{base64.b64encode(data).decode('utf-8')}"


def chat_image_part(image: Image) -> dict[str, Any] | None:
    """Return a Chat Completions ``image_url`` part, or None when the image cannot be read."""
    url = _image_url(image)
    if url is None:
        return None
    part: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
    if image.detail:
        part["image_url"]["detail"] = image.detail
    return part


def responses_image_part(image: Image) -> dict[str, Any] | None:
    """Return a Responses ``input_image`` part, with ``detail`` at the top level as that API expects."""
    url = _image_url(image)
    if url is None:
        return None
    part: dict[str, Any] = {"type": "input_image", "image_url": url}
    if image.detail:
        part["detail"] = image.detail
    return part


def chat_audio_part(audio: Audio) -> dict[str, Any] | None:
    """Return a Chat Completions ``input_audio`` part for local audio, or None."""
    if audio.content:
        data, audio_format = audio.content, audio.format or "wav"
    elif audio.filepath is not None:
        data = _file_bytes(audio.filepath)
        audio_format = audio.format or Path(audio.filepath).suffix.lstrip(".")
    else:
        logger.warning("Remote audio input is not fetched", audio_id=audio.id)
        return None
    if data is None or not audio_format:
        return None
    return {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(data).decode("utf-8"), "format": audio_format},
    }


def _content_bytes(file: File) -> bytes | None:
    if file.filepath is not None:
        return _file_bytes(file.filepath)
    if isinstance(file.content, str):
        return file.content.encode("utf-8")
    return file.content


def chat_file_part(file: File) -> dict[str, Any] | None:
    """Return a Chat Completions ``file`` part with an inline data URL, or None."""
    if file.filepath is not None:
        name = Path(file.filepath).name
    elif file.content is not None:
        name = file.filename or "file"
    else:
        logger.warning("Remote file input is not fetched", file_id=file.id)
        return None
    data = _content_bytes(file)
    if data is None:
        return None
    mime_type = file.mime_type or mimetypes.guess_type(name)[0] or "application/pdf"
    data_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('utf-8')}"
    return {"type": "file", "file": {"filename": name, "file_data": data_url}}


def _responses_file_mime_type(file: File, filename: str) -> str:
    """Prefer the original filename over an opaque storage path, which may end in ``.bin``."""
    if file.mime_type:
        return file.mime_type
    display_name = file.filename or file.name
    if display_name:
        guessed, encoding = mimetypes.guess_type(display_name)
        guessed = _RESPONSES_FILE_MIME_TYPES.get(Path(display_name).suffix.lower(), guessed)
        if guessed and encoding is None:
            return guessed
    source = file.filepath or filename
    return mimetypes.guess_type(source)[0] or "application/octet-stream"


def responses_file_part(file: File) -> dict[str, Any] | None:
    """Return a Responses ``input_file`` part, a URL reference or inline data, or None for a file without either."""
    filename = file.filename or file.name or (Path(file.filepath).name if file.filepath else None) or "document"
    if file.url:
        return {"type": "input_file", "file_url": file.url}
    data = _content_bytes(file)
    if data is None:
        return None
    data_url = f"data:{_responses_file_mime_type(file, filename)};base64,{base64.b64encode(data).decode('utf-8')}"
    return {"type": "input_file", "filename": filename, "file_data": data_url}
