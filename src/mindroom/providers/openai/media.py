# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Inline media content parts for Chat Completions and Responses requests.

Attachments are filepath-backed, so parts are read and base64 encoded here; a file that cannot be read raises
``InvalidRequest``, and an empty one adds no part (``read_media_file``); ``provider`` names the adapter in that error.
Image URLs, and file URLs on Responses, go for the provider to fetch; the pipeline downloaded every other URL
(``Capabilities.url_media``).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.llm.media import image_mime_type
from mindroom.providers.media_files import read_media_bytes

if TYPE_CHECKING:
    from mindroom.llm.media import Audio, File, Image

__all__ = [
    "chat_audio_part",
    "chat_file_part",
    "chat_image_part",
    "file_part_name",
    "responses_file_part",
    "responses_image_part",
]

# Python's MIME database lacks or misnames these supported formats; host /etc/mime.types must not decide.
_FILE_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".sh": "text/x-sh",
}


def _image_url(image: Image, provider: str) -> str | None:
    if image.url is not None:
        return image.url
    if image.filepath is None:
        data = image.content
    else:
        data = read_media_bytes(image.filepath, media_id=image.id, kind="image", provider=provider)
    if data is None:
        return None
    return f"data:{image_mime_type(image, data)};base64,{base64.b64encode(data).decode('utf-8')}"


def chat_image_part(image: Image, *, provider: str) -> dict[str, Any] | None:
    """Return a Chat Completions ``image_url`` part, or None for an empty file."""
    url = _image_url(image, provider)
    if url is None:
        return None
    part: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
    if image.detail:
        part["image_url"]["detail"] = image.detail
    return part


def responses_image_part(image: Image, *, provider: str) -> dict[str, Any] | None:
    """Return a Responses ``input_image`` part, with ``detail`` at the top level as that API expects."""
    url = _image_url(image, provider)
    if url is None:
        return None
    part: dict[str, Any] = {"type": "input_image", "image_url": url}
    if image.detail:
        part["detail"] = image.detail
    return part


def chat_audio_part(audio: Audio, *, provider: str) -> dict[str, Any] | None:
    """Return a Chat Completions ``input_audio`` part, or None for audio without bytes or a format."""
    if audio.content:
        data, audio_format = audio.content, audio.format or "wav"
    elif audio.filepath is not None:
        data = read_media_bytes(audio.filepath, media_id=audio.id, kind="audio", provider=provider)
        audio_format = audio.format or Path(audio.filepath).suffix.lstrip(".")
    else:
        return None
    if data is None or not audio_format:
        return None
    return {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(data).decode("utf-8"), "format": audio_format},
    }


def _content_bytes(file: File, provider: str) -> bytes | None:
    if file.filepath is not None:
        return read_media_bytes(file.filepath, media_id=file.id, kind="file", provider=provider)
    if isinstance(file.content, str):
        return file.content.encode("utf-8")
    return file.content


def file_part_name(file: File, *, chat: bool) -> str:
    """Return the name a Chat (``chat``) or Responses file part carries for ``file``.

    That is the attachment's original filename, else its stored file's name, else the wire's generic name.
    """
    fallback = "file" if chat else "document"
    return file.filename or file.name or (Path(file.filepath).name if file.filepath else None) or fallback


def _file_part_data_url(file: File, name: str, data: bytes) -> str:
    """Return a file part's data URL, typed by the declared type, else by the original filename, then the stored path.

    The original filename comes first because a storage path is opaque and may end in ``.bin``. A declared
    ``application/octet-stream`` names no type, and Responses rejects it, so the filename decides then too; a
    compressed name such as ``report.csv.gz`` names no type either.
    """
    mime_type = file.mime_type if file.mime_type and file.mime_type != "application/octet-stream" else None
    display_name = file.filename or file.name
    if mime_type is None and display_name:
        guessed, encoding = mimetypes.guess_type(display_name)
        guessed = _FILE_MIME_TYPES.get(Path(display_name).suffix.lower(), guessed)
        mime_type = guessed if encoding is None else None
    if mime_type is None:
        guessed, encoding = mimetypes.guess_type(file.filepath or name)
        mime_type = guessed if guessed and encoding is None else "application/octet-stream"
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('utf-8')}"


def chat_file_part(file: File, *, provider: str) -> dict[str, Any] | None:
    """Return a Chat Completions ``file`` part with an inline data URL, or None for a file without bytes or an empty one.

    It is named and typed as a Responses file part is (``file_part_name``, ``_file_part_data_url``).
    """
    name = file_part_name(file, chat=True)
    data = _content_bytes(file, provider)
    if data is None:
        return None
    return {"type": "file", "file": {"filename": name, "file_data": _file_part_data_url(file, name, data)}}


def responses_file_part(file: File, *, provider: str) -> dict[str, Any] | None:
    """Return a Responses ``input_file`` part, a URL reference or inline data, or None when neither is available."""
    name = file_part_name(file, chat=False)
    if file.url:
        return {"type": "input_file", "file_url": file.url}
    data = _content_bytes(file, provider)
    if data is None:
        return None
    return {"type": "input_file", "filename": name, "file_data": _file_part_data_url(file, name, data)}
