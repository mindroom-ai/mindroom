"""Plain-data protocol for the native knowledge reader."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter

MAX_FRAME_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ReadRequest:
    """Read an exact collection or its compatible current publication; no credentials cross IPC."""

    path: str
    collection: str
    query: str | None = None
    embedding: list[float] | None = None
    limit: int = 5
    published_settings: dict[str, str] | None = None


@dataclass(frozen=True)
class SearchHit:
    """One stored chunk a search found, as the knowledge search tool shows it to the model."""

    content: str
    #: The source file's basename the chunk was stored under.
    name: str | None = None
    #: Every stored key except ``name`` and ``content_id``, then the hit's cosine distance as ``distances`` and
    #: ``similarity_score``.
    meta_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReadResult:
    """Existence or search hits, with an explicit native failure classification."""

    exists: bool = False
    hits: list[SearchHit] = field(default_factory=list)
    error_type: str | None = None


request_adapter = TypeAdapter(ReadRequest)
result_adapter = TypeAdapter(ReadResult)
