# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Media attached to messages, with dict codecs that match the stored Agno 3 shapes.

Every kind keeps Agno's scalar field names so stored runs, summary-row segments, tool-job payloads,
and the worker media envelope decode without migration.
``content`` travels as base64 text in dicts; ``File`` keeps Agno's exception of writing ``text/*``
content as UTF-8 text.
"""

from __future__ import annotations

import base64
import mimetypes
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any
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
    # Agno decoded every stored ``content`` string this way, falling back to the raw UTF-8 bytes.
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


def _stored_dict(media: _Media, *, content: str | None) -> dict[str, Any]:
    """Return the stored dict: every field in declaration order but ``content`` and ``metadata``, which come last.

    None values and an empty ``metadata`` are left out.
    """
    values = {item.name: getattr(media, item.name) for item in fields(media) if item.name not in _LAST_KEYS}
    values.update(content=content, metadata=media.metadata or None)
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


@dataclass(frozen=True, slots=True, kw_only=True)
class File:
    """A document, usually given by ``url``, ``filepath``, or ``content``.

    Agno accepted a file identified by its id alone or by a provider-side ``external`` object, and stored
    runs hold such files, so no source is required; the owned ``id`` always identifies it.
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

    def _encoded_content(self) -> str | None:
        if not self.content:
            return None
        if isinstance(self.content, str):
            return self.content
        if _is_text(self.mime_type):
            try:
                return self.content.decode("utf-8")
            except UnicodeDecodeError:
                pass
        return base64.b64encode(self.content).decode("utf-8")

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        """Return the stored dict shape; ``text/*`` bytes that are UTF-8 become text, other bytes base64 text."""
        return _stored_dict(self, content=self._encoded_content() if include_content else None)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> File:
        """Rebuild from a stored dict; text content decodes by ``mime_type`` as Agno wrote it."""
        raw = data.get("content")
        content: bytes | None = None
        if isinstance(raw, str):
            # TODO(simplify): ``text/*`` bytes that are not UTF-8 are written as base64 but read back as that base64 text.
            # Write them so the reader can tell, such as always as base64, so they read back as their bytes.
            # Users and stored data would notice: such an attachment after an approval resume. (#3075)
            content = raw.encode("utf-8") if _is_text(data.get("mime_type")) else _decode_base64(raw)
        return cls(id=_id_from(data), content=content, **_stored_fields(cls, data))


__all__ = [
    "IMAGE_HEADER_BYTES",
    "VIEWED_IMAGE_ID_PREFIX",
    "Audio",
    "File",
    "Image",
    "Video",
    "canonical_mime_type",
    "image_format",
    "image_mime_type",
]
