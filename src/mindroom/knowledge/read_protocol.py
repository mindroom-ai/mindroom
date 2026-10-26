"""Plain-data protocol for the native knowledge reader."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter

MAX_FRAME_BYTES = 16 * 1024 * 1024
#: The stored chunk metadata key, and so the hit ``meta_data`` key, naming the chunk's source file relative to its base.
SOURCE_PATH_KEY = "source_path"
#: Source signature every indexed chunk carries beside its ``SOURCE_PATH_KEY``, so a collection doubles as an
#: index from source path to the vectors and signature that path produced.
SOURCE_MTIME_NS_KEY = "source_mtime_ns"
SOURCE_SIZE_KEY = "source_size"
SOURCE_DIGEST_KEY = "source_digest"
#: The hit ``meta_data`` key holding the chunk's cosine distance from the query, smaller for a closer match.
DISTANCE_KEY = "distance"


@dataclass(frozen=True)
class ReadRequest:
    """Read an exact collection or its compatible current publication; no credentials cross IPC."""

    path: str
    collection: str
    #: The search query's embedding.
    embedding: list[float]
    limit: int = 5
    published_settings: dict[str, str] | None = None


@dataclass(frozen=True)
class SearchHit:
    """One stored chunk a search found, as the knowledge search tool shows it to the model."""

    content: str
    #: The source file's basename the chunk was stored under.
    name: str | None = None
    #: Where the chunk comes from and what the reader recorded of it, such as ``source_path``, ``chunk``, ``page``, or
    #: ``row_number``, then the hit's ``DISTANCE_KEY``.
    meta_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReadResult:
    """Search hits, or an explicit native failure classification."""

    hits: list[SearchHit] = field(default_factory=list)
    error_type: str | None = None


request_adapter = TypeAdapter(ReadRequest)
result_adapter = TypeAdapter(ReadResult)
