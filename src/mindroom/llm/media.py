# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Media attached to messages, with the dict codecs of their stored shapes.

Field names are the keys stored message rows, tool-job payloads, and the worker media envelope hold, and those of the
Agno-layout runs only the storage v2 conversion still reads. ``content`` travels as base64 text in dicts, except that ``File`` writes UTF-8
``text/*`` content as text and marks a ``text/*`` file's base64 content with ``content_encoding``.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass, field, fields, replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping

# Images that ``get_attachment(view=True)`` produced carry ids with this prefix, so a route that rejects image
# input can mark their tool receipts as unsupported.
VIEWED_IMAGE_ID_PREFIX = "mindroom_viewed_"
# How many header bytes identify an image format, and the formats by their header: (offset, signature, format).
IMAGE_HEADER_BYTES = 12
_IMAGE_SIGNATURES = (
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\xff\xd8\xff", "jpeg"),
    (4, b"ftyp", "heic"),
)


def _new_id() -> str:
    return str(uuid4())


def _id_from(data: Mapping[str, Any]) -> str:
    identifier = data.get("id")
    return identifier if isinstance(identifier, str) else _new_id()


def _require_one_source(url: str | None, filepath: str | None, content: bytes | None) -> None:
    sources = [source for source in (url, filepath, content) if source is not None]
    if not sources:
        msg = "One of 'url', 'filepath', or 'content' must be provided"
        raise ValueError(msg)
    if len(sources) > 1:
        msg = "Only one of 'url', 'filepath', or 'content' should be provided"
        raise ValueError(msg)


def _decode_base64(text: str) -> bytes:
    # Text that fails to decode as base64 reads as its UTF-8 bytes.
    try:
        return base64.b64decode(text)
    except Exception:
        return text.encode("utf-8")


def _bytes_from(data: Mapping[str, Any]) -> bytes | None:
    content = data.get("content")
    return _decode_base64(content) if isinstance(content, str) else None


def _base64_or_none(content: bytes | None, *, include_content: bool) -> str | None:
    if include_content and content:
        return base64.b64encode(content).decode("utf-8")
    return None


type _Media = Image | Audio | Video | File
_LAST_KEYS = ("content", "metadata")
# A ``text/*`` file's stored content reads as UTF-8 text unless this key says it is base64.
_CONTENT_ENCODING_KEY = "content_encoding"
_BASE64 = "base64"


def _stored_dict(media: _Media, *, content: str | None, content_encoding: str | None = None) -> dict[str, Any]:
    """Return the stored dict: every field in declaration order but ``content`` and ``metadata``, which come last.

    ``content_encoding``, when given, follows ``content``. None values and an empty ``metadata`` are left out.
    """
    values = {item.name: getattr(media, item.name) for item in fields(media) if item.name not in _LAST_KEYS}
    values.update({"content": content, _CONTENT_ENCODING_KEY: content_encoding, "metadata": media.metadata or None})
    return {key: value for key, value in values.items() if value is not None}


def _stored_fields(cls: type[_Media], data: Mapping[str, Any]) -> dict[str, Any]:
    """Read every field but ``id`` and ``content`` from a stored dict, None when absent; other keys are ignored."""
    return {item.name: data.get(item.name) for item in fields(cls) if item.name not in {"id", "content"}}


@dataclass(frozen=True, slots=True, kw_only=True)
class Image:
    """An image given to or produced for the model; exactly one of ``url``, ``filepath``, ``content``."""

    id: str = field(default_factory=_new_id)
    url: str | None = None
    filepath: str | None = None
    content: bytes | None = None
    format: str | None = None
    mime_type: str | None = None
    detail: str | None = None
    original_prompt: str | None = None
    revised_prompt: str | None = None
    alt_text: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Reject images with no source or more than one."""
        _require_one_source(self.url, self.filepath, self.content)

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        """Return the stored dict shape; ``content`` is base64 text and None values are dropped."""
        return _stored_dict(self, content=_base64_or_none(self.content, include_content=include_content))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Image:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(id=_id_from(data), content=_bytes_from(data), **_stored_fields(cls, data))


@dataclass(frozen=True, slots=True, kw_only=True)
class Audio:
    """An audio clip; exactly one of ``url``, ``filepath``, ``content``."""

    id: str = field(default_factory=_new_id)
    url: str | None = None
    filepath: str | None = None
    content: bytes | None = None
    format: str | None = None
    mime_type: str | None = None
    duration: float | None = None
    sample_rate: int | None = 24000
    channels: int | None = 1
    transcript: str | None = None
    expires_at: int | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Reject clips with no source or more than one."""
        _require_one_source(self.url, self.filepath, self.content)

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        """Return the stored dict shape; ``content`` is base64 text and None values are dropped."""
        return _stored_dict(self, content=_base64_or_none(self.content, include_content=include_content))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Audio:
        """Rebuild from a stored dict, ignoring keys this type does not model; a missing rate or channel count is None."""
        return cls(id=_id_from(data), content=_bytes_from(data), **_stored_fields(cls, data))


@dataclass(frozen=True, slots=True, kw_only=True)
class Video:
    """A video; exactly one of ``url``, ``filepath``, ``content``."""

    id: str = field(default_factory=_new_id)
    url: str | None = None
    filepath: str | None = None
    content: bytes | None = None
    format: str | None = None
    mime_type: str | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    eta: str | None = None
    original_prompt: str | None = None
    revised_prompt: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Reject videos with no source or more than one."""
        _require_one_source(self.url, self.filepath, self.content)

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        """Return the stored dict shape; ``content`` is base64 text and None values are dropped."""
        return _stored_dict(self, content=_base64_or_none(self.content, include_content=include_content))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Video:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(id=_id_from(data), content=_bytes_from(data), **_stored_fields(cls, data))


def image_format(data: bytes) -> str | None:
    """Return the image format named by the file header's magic bytes, or None when they name none."""
    if len(data) < IMAGE_HEADER_BYTES:
        return None
    if data[0:4] == b"GIF8" and data[5:6] == b"a":
        return "gif"
    if data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((name for offset, signature, name in _IMAGE_SIGNATURES if data[offset:].startswith(signature)), None)


def canonical_mime_type(mime_type: str | None) -> str | None:
    """Return ``mime_type``, such as a Content-Type header, as a lowercase ``type/subtype`` without parameters.

    None when it names no type.
    """
    if not isinstance(mime_type, str):
        return None
    normalized = mime_type.split(";", 1)[0].strip().lower()
    return normalized or None


# The longest attachment name or type that model-facing text prints.
_MAX_RENDERED_LABEL_LENGTH = 80
_RENDERED_LABEL_REPLACEMENTS = str.maketrans({'"': "'", "<": "\u2039", ">": "\u203a"})


def rendered_label(value: str) -> str:
    """Return a sender-chosen attachment name or type as text the model reads, so it cannot forge lines, quotes or tags.

    Characters that do not print, such as newlines, are dropped, double quotes become single ones, angle brackets
    become single angle quotation marks, so no ``<msg>`` tag can form, and long values are cut short.
    """
    sanitized = "".join(char for char in value if char.isprintable()).translate(_RENDERED_LABEL_REPLACEMENTS).strip()
    if len(sanitized) > _MAX_RENDERED_LABEL_LENGTH:
        sanitized = f"{sanitized[: _MAX_RENDERED_LABEL_LENGTH - 1]}…"
    return sanitized


# The audio formats chat audio inputs name, by the MIME types that mean them.
_AUDIO_FORMATS = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
}
# Audio formats a file name's suffix names as they are.
_AUDIO_SUFFIX_FORMATS = frozenset({"mp3", "wav", "ogg", "webm"})


def url_file_name(url: str) -> str:
    """Return the file name a URL's path ends in, decoded, or ``""`` when it ends in none."""
    return PurePosixPath(unquote(urlsplit(url).path)).name


def _audio_format(mime_type: str | None) -> str | None:
    """Return the audio format, such as ``mp3``, that ``mime_type`` means, or None for a type with no such format."""
    return _AUDIO_FORMATS.get(canonical_mime_type(mime_type) or "")


def image_mime_type(image: Image, data: bytes) -> str:
    """Return an image's MIME type: its own, else its format, path extension, or header bytes, else JPEG.

    ``data`` holds the image's bytes, or at least its first ``IMAGE_HEADER_BYTES``.
    """
    if image.mime_type:
        return image.mime_type
    if image.format:
        return f"image/{image.format.lower()}"
    if image.filepath and (guessed := mimetypes.guess_type(image.filepath)[0]):
        return guessed
    detected = image_format(data)
    return f"image/{detected}" if detected else "image/jpeg"


def _is_text(mime_type: str | None) -> bool:
    return mime_type is not None and mime_type.startswith("text/")


# The document types a tool may return a file as for the model to read; a model's provider adapter decides which of
# them it sends.
DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/json",
        "application/x-javascript",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/msword",
        "application/vnd.ms-powerpoint",
        "application/vnd.ms-excel",
        "application/vnd.ms-outlook",
        "application/zip",
        "message/rfc822",
        "text/javascript",
        "application/x-python",
        "text/x-python",
        "text/plain",
        "text/html",
        "text/css",
        "text/markdown",
        "text/csv",
        "text/xml",
        "text/rtf",
    },
)


@dataclass(frozen=True, slots=True, kw_only=True)
class File:
    """A document, usually given by ``url``, ``filepath``, or ``content``.

    Stored runs hold files identified by their id alone or by a provider-side ``external`` object, so no source is
    required; the ``id`` always identifies it.
    ``mime_type`` is free text: the stored-row readers never rejected any value, and the provider
    adapters decide what each model accepts.
    """

    id: str = field(default_factory=_new_id)
    url: str | None = None
    filepath: str | None = None
    content: bytes | str | None = None
    mime_type: str | None = None
    file_type: str | None = None
    filename: str | None = None
    size: int | None = None
    format: str | None = None
    name: str | None = None
    metadata: dict[str, Any] | None = None

    def _stored_content(self) -> tuple[str | None, str | None]:
        """Return the stored ``content`` and its ``content_encoding``, which only a ``text/*`` file's base64 needs."""
        if not self.content:
            return None, None
        if isinstance(self.content, str):
            return self.content, None
        if not _is_text(self.mime_type):
            return base64.b64encode(self.content).decode("utf-8"), None
        try:
            return self.content.decode("utf-8"), None
        except UnicodeDecodeError:
            return base64.b64encode(self.content).decode("utf-8"), _BASE64

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        """Return the stored dict shape; ``text/*`` bytes that are UTF-8 become text, other bytes base64 text."""
        content, encoding = self._stored_content() if include_content else (None, None)
        return _stored_dict(self, content=content, content_encoding=encoding)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> File:
        """Rebuild from a stored dict; ``text/*`` content reads as UTF-8 text unless marked base64, other as base64."""
        raw = data.get("content")
        content: bytes | None = None
        if isinstance(raw, str):
            # LEGACY_COMPAT: Unmarked base64 content of a text file that is not UTF-8.
            # Legacy format: Agno and MindRoom stored a `text/*` file whose bytes are not UTF-8 as base64 text without
            # `content_encoding`, the same shape as a UTF-8 text file's text.
            # Last legacy release: v2026.10.541; replacement: v2026.10.542 adds `content_encoding: "base64"`.
            # Handling: Unmarked `text/*` content reads as UTF-8 text, so such an earlier file still reads as its
            # base64 text; no rule tells it apart from a text file whose text is valid base64.
            # Coverage: tests/llm/test_codecs.py::test_a_text_file_stored_before_its_encoding_was_recorded_reads_as_before.
            as_text = _is_text(data.get("mime_type")) and data.get(_CONTENT_ENCODING_KEY) != _BASE64
            content = raw.encode("utf-8") if as_text else _decode_base64(raw)
        return cls(id=_id_from(data), content=content, **_stored_fields(cls, data))


# The MIME type family of each media kind that a file name or a declared type can name.
MEDIA_MIME_FAMILIES: dict[type[Image | Audio | Video], str] = {Image: "image/", Audio: "audio/", Video: "video/"}


def _named_type(name: str) -> str | None:
    """Return the type a file name names; a compressed name, such as ``report.csv.gz``, names none."""
    named, encoding = mimetypes.guess_type(name)
    return named if encoding is None else None


def named_by_resource[T: Image | Audio | Video | File](
    media: T,
    resource_name: str,
    declared_mime: str | None = None,
) -> T:
    """Return ``media`` with the type, and for a file the filename, the resource its bytes came from gives it.

    ``resource_name`` is the last part of that resource's path or URL, and ``declared_mime`` the type its server
    declared. A type, format, or filename the item already has stands. A file takes the first of the declared type,
    its filename's, and the resource name's that is in ``DOCUMENT_MIME_TYPES``. Other media without a type or format
    takes the declared type when it is of its kind, an image also its resource name's; audio and video take the
    name's suffix as their format, and audio whose suffix names no audio format the format its type means, such as
    ``mp3``.
    """
    if isinstance(media, File):
        filename = media.filename or media.name or resource_name or None
        candidates = (declared_mime, _named_type(filename or ""), _named_type(resource_name))
        mime_type = media.mime_type or next((value for value in candidates if value in DOCUMENT_MIME_TYPES), None)
        return replace(media, filename=filename, mime_type=mime_type)
    mime_type, media_format = media.mime_type, media.format
    named_type = _named_type(resource_name)
    if mime_type is None and media_format is None:
        family = MEDIA_MIME_FAMILIES[type(media)]
        candidates = (declared_mime, named_type if isinstance(media, Image) else None)
        mime_type = next((value for value in candidates if value and value.startswith(family)), None)
    if not isinstance(media, Image) and media.format is None:
        media_format = PurePosixPath(resource_name).suffix.removeprefix(".").lower() or None
        if (
            isinstance(media, Audio)
            and media_format not in _AUDIO_SUFFIX_FORMATS
            and not (named_type or "").startswith("audio/")
        ):
            media_format = _audio_format(mime_type) or media_format
    return replace(media, mime_type=mime_type, format=media_format)


__all__ = [
    "DOCUMENT_MIME_TYPES",
    "IMAGE_HEADER_BYTES",
    "MEDIA_MIME_FAMILIES",
    "VIEWED_IMAGE_ID_PREFIX",
    "Audio",
    "File",
    "Image",
    "Video",
    "canonical_mime_type",
    "image_format",
    "image_mime_type",
    "named_by_resource",
    "rendered_label",
    "url_file_name",
]
