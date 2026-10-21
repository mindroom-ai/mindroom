# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Media attached to messages, with dict codecs that match the stored Agno 3 shapes.

Every kind keeps Agno's scalar field names so stored runs, summary-row segments, tool-job payloads,
and the worker media envelope decode without migration.
``content`` travels as base64 text in dicts; ``File`` keeps Agno's exception of writing ``text/*``
content as UTF-8 text.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Mapping


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
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return _decode_base64(content)
    return None


def _finish(scalars: dict[str, Any], *, content: str | None, metadata: dict[str, Any] | None) -> dict[str, Any]:
    if content is not None:
        scalars["content"] = content
    if metadata:
        scalars["metadata"] = metadata
    return {key: value for key, value in scalars.items() if value is not None}


def _base64_or_none(content: bytes | None, *, include_content: bool) -> str | None:
    if include_content and content:
        return base64.b64encode(content).decode("utf-8")
    return None


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
        scalars = {
            "id": self.id,
            "url": self.url,
            "filepath": self.filepath,
            "format": self.format,
            "mime_type": self.mime_type,
            "detail": self.detail,
            "original_prompt": self.original_prompt,
            "revised_prompt": self.revised_prompt,
            "alt_text": self.alt_text,
        }
        content = _base64_or_none(self.content, include_content=include_content)
        return _finish(scalars, content=content, metadata=self.metadata)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Image:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(
            id=_id_from(data),
            url=data.get("url"),
            filepath=data.get("filepath"),
            content=_bytes_from(data),
            format=data.get("format"),
            mime_type=data.get("mime_type"),
            detail=data.get("detail"),
            original_prompt=data.get("original_prompt"),
            revised_prompt=data.get("revised_prompt"),
            alt_text=data.get("alt_text"),
            metadata=data.get("metadata"),
        )


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
        scalars = {
            "id": self.id,
            "url": self.url,
            "filepath": self.filepath,
            "format": self.format,
            "mime_type": self.mime_type,
            "duration": self.duration,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "transcript": self.transcript,
            "expires_at": self.expires_at,
        }
        content = _base64_or_none(self.content, include_content=include_content)
        return _finish(scalars, content=content, metadata=self.metadata)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Audio:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(
            id=_id_from(data),
            url=data.get("url"),
            filepath=data.get("filepath"),
            content=_bytes_from(data),
            format=data.get("format"),
            mime_type=data.get("mime_type"),
            duration=data.get("duration"),
            sample_rate=data.get("sample_rate"),
            channels=data.get("channels"),
            transcript=data.get("transcript"),
            expires_at=data.get("expires_at"),
            metadata=data.get("metadata"),
        )


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
        scalars = {
            "id": self.id,
            "url": self.url,
            "filepath": self.filepath,
            "format": self.format,
            "mime_type": self.mime_type,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "eta": self.eta,
            "original_prompt": self.original_prompt,
            "revised_prompt": self.revised_prompt,
        }
        content = _base64_or_none(self.content, include_content=include_content)
        return _finish(scalars, content=content, metadata=self.metadata)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Video:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        return cls(
            id=_id_from(data),
            url=data.get("url"),
            filepath=data.get("filepath"),
            content=_bytes_from(data),
            format=data.get("format"),
            mime_type=data.get("mime_type"),
            duration=data.get("duration"),
            width=data.get("width"),
            height=data.get("height"),
            fps=data.get("fps"),
            eta=data.get("eta"),
            original_prompt=data.get("original_prompt"),
            revised_prompt=data.get("revised_prompt"),
            metadata=data.get("metadata"),
        )


def _is_text(mime_type: str | None) -> bool:
    return mime_type is not None and mime_type.startswith("text/")


@dataclass(frozen=True, slots=True, kw_only=True)
class File:
    """A document; at least one of ``url``, ``filepath``, ``content``.

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

    def __post_init__(self) -> None:
        """Reject files with no source."""
        if self.url is None and self.filepath is None and self.content is None:
            msg = "At least one of 'url', 'filepath', or 'content' must be provided"
            raise ValueError(msg)

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
        """Return the stored dict shape; ``text/*`` bytes become UTF-8 text, other bytes base64 text."""
        scalars = {
            "id": self.id,
            "url": self.url,
            "filepath": self.filepath,
            "mime_type": self.mime_type,
            "file_type": self.file_type,
            "filename": self.filename,
            "size": self.size,
            "format": self.format,
            "name": self.name,
        }
        content = self._encoded_content() if include_content else None
        return _finish(scalars, content=content, metadata=self.metadata)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> File:
        """Rebuild from a stored dict; text content decodes by ``mime_type`` as Agno wrote it."""
        mime_type = data.get("mime_type")
        raw = data.get("content")
        content: bytes | None
        if isinstance(raw, bytes):
            content = raw
        elif isinstance(raw, str):
            content = raw.encode("utf-8") if _is_text(mime_type) else _decode_base64(raw)
        else:
            content = None
        return cls(
            id=_id_from(data),
            url=data.get("url"),
            filepath=data.get("filepath"),
            content=content,
            mime_type=mime_type,
            file_type=data.get("file_type"),
            filename=data.get("filename"),
            size=data.get("size"),
            format=data.get("format"),
            name=data.get("name"),
            metadata=data.get("metadata"),
        )


__all__ = [
    "Audio",
    "File",
    "Image",
    "Video",
]
