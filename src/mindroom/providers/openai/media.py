# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Inline media content parts for Chat Completions and Responses requests.

Attachments are filepath-backed, so parts are read and base64 encoded here; a file that cannot be read raises
``InvalidRequest``, and an empty one adds no part (``read_media_file``); ``provider`` names the adapter in that error.
A file of a type the API does not read goes unread as a text part noting it (``unreadable_file_note``). Image URLs,
and file URLs on Responses, go for the provider to fetch; the pipeline downloaded every other URL
(``Capabilities.url_media``).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.llm.media import image_mime_type
from mindroom.providers.media_files import (
    OPENAI_TEXT_TYPES,
    file_bytes,
    file_mime_type,
    file_name,
    file_text,
    has_file_bytes,
    is_text_mime_type,
    read_media_bytes,
    unreadable_file_note,
)

if TYPE_CHECKING:
    from mindroom.llm.media import Audio, File, Image

__all__ = [
    "chat_audio_part",
    "chat_file_part",
    "chat_image_part",
    "responses_file_part",
    "responses_image_part",
]

# OpenAI's file inputs guide (https://developers.openai.com/api/docs/guides/pdf-files): "Chat Completions accepts only
# PDF files as ``file`` content parts", and the guide sends another file's text as a ``text`` part. Responses reads the
# types of the guide's "Full list of accepted file types": PDF, the Office and other document formats below, and text,
# which is any ``text/*`` type, covering the ones the list names, and the list's text types outside ``text/``.
_PDF = "application/pdf"
_RESPONSES_FILE_TYPES = OPENAI_TEXT_TYPES | {
    _PDF,
    # Spreadsheets.
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/vnd.google-apps.spreadsheet",
    # Word and rich documents.
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.apple.pages",
    "application/vnd.google-apps.document",
    "application/vnd.apple.iwork",
    # Presentations.
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "application/vnd.apple.keynote",
    "application/vnd.google-apps.presentation",
}


def _data_url(mime_type: str, data: bytes) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('utf-8')}"


def _image_url(image: Image, provider: str) -> str | None:
    if image.url is not None:
        return image.url
    data = (
        image.content if image.filepath is None else read_media_bytes(image.filepath, kind="image", provider=provider)
    )
    return None if data is None else _data_url(image_mime_type(image, data), data)


def chat_image_part(image: Image, *, provider: str) -> dict[str, Any] | None:
    """Return a Chat Completions ``image_url`` part, or None for an image without a source."""
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
        data = read_media_bytes(audio.filepath, kind="audio", provider=provider)
        audio_format = audio.format or Path(audio.filepath).suffix.lstrip(".")
    else:
        return None
    if data is None or not audio_format:
        return None
    return {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(data).decode("utf-8"), "format": audio_format},
    }


def chat_file_part(file: File, *, provider: str) -> dict[str, Any] | None:
    """Return a Chat Completions ``file`` part for a PDF, a text part for a text file, or None.

    A text file goes as its text (``file_text``), and a file of another type as a text part noting it. None is for a
    file without bytes or an empty local file. A file part is named and typed as a Responses file part is.
    """
    if not has_file_bytes(file):
        return None
    mime_type = file_mime_type(file)
    text_file = is_text_mime_type(mime_type)
    if mime_type != _PDF and not text_file:
        return {"type": "text", "text": unreadable_file_note(file, mime_type)}
    if (data := file_bytes(file, provider=provider)) is None:
        return None
    if text_file:
        return {"type": "text", "text": file_text(data)}
    return {
        "type": "file",
        "file": {"filename": file_name(file, "file"), "file_data": _data_url(mime_type, data)},
    }


def responses_file_part(file: File, *, provider: str) -> dict[str, Any] | None:
    """Return a Responses ``input_file`` part, a URL reference or inline data, or None when neither is available.

    Responses fetches and types a file URL itself; inline data of a type it does not read goes as a text part noting it,
    and an empty local file has no data.
    """
    if file.url:
        return {"type": "input_file", "file_url": file.url}
    if not has_file_bytes(file):
        return None
    mime_type = file_mime_type(file)
    if mime_type not in _RESPONSES_FILE_TYPES and not mime_type.startswith("text/"):
        return {"type": "input_text", "text": unreadable_file_note(file, mime_type)}
    if (data := file_bytes(file, provider=provider)) is None:
        return None
    return {
        "type": "input_file",
        "filename": file_name(file, "document"),
        "file_data": _data_url(mime_type, data),
    }
