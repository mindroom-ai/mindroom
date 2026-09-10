"""Plain-data protocol for the native knowledge reader."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

if TYPE_CHECKING:
    from agno.knowledge.document import Document

MAX_FRAME_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ReadRequest:
    """Read one exact published collection; no provider credentials cross IPC."""

    path: str
    collection: str
    query: str | None = None
    embedding: list[float] | None = None
    limit: int = 5
    filters: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReadDocument:
    """Complete search result without an executable embedder reference."""

    content: str
    id: str | None = None
    name: str | None = None
    meta_data: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None
    usage: dict[str, Any] | None = None
    reranking_score: float | None = None
    content_id: str | None = None
    content_origin: str | None = None
    size: int | None = None

    @classmethod
    def from_document(cls, document: Document) -> ReadDocument:
        """Preserve Agno fields, normalizing native vectors to Python numbers."""
        return cls(
            content=document.content,
            id=document.id,
            name=document.name,
            meta_data=document.meta_data,
            embedding=[float(value) for value in document.embedding] if document.embedding is not None else None,
            usage=document.usage,
            reranking_score=document.reranking_score,
            content_id=document.content_id,
            content_origin=document.content_origin,
            size=document.size,
        )


@dataclass(frozen=True)
class ReadResult:
    """Existence or documents, with an explicit native failure classification."""

    exists: bool = False
    documents: list[ReadDocument] = field(default_factory=list)
    error_type: str | None = None


request_adapter = TypeAdapter(ReadRequest)
result_adapter = TypeAdapter(ReadResult)
