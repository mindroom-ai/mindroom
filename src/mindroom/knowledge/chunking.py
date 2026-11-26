"""MindRoom-owned document model and chunking strategies for knowledge indexing."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _legacy_clean_text(text: str) -> str:
    """Collapse each whitespace run, line breaks included, to one space, as every indexed chunk has so far.

    Default chunkers keep line breaks, which would change the text and id of every chunk a refresh rewrites.
    """
    return re.sub(r"\s+", " ", text)


@dataclass
class Document:
    """One extracted or chunked document."""

    content: str
    id: str | None = None
    name: str | None = None
    meta_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable dictionary representation of this document."""
        result: dict[str, Any] = {}
        if self.name is not None:
            result["name"] = self.name
        if self.meta_data is not None:
            result["meta_data"] = self.meta_data
        result["content"] = self.content
        return result


class ChunkingStrategy(ABC):
    """Base class for splitting a ``Document`` into chunks."""

    @abstractmethod
    def chunk(self, document: Document) -> list[Document]:
        """Split ``document`` into chunk documents."""

    def _generate_chunk_id(
        self,
        document: Document,
        chunk_number: int,
        content: str | None = None,
        prefix: str | None = None,
    ) -> str | None:
        suffix = f"_{prefix}_{chunk_number}" if prefix else f"_{chunk_number}"
        if document.id:
            return f"{document.id}{suffix}"
        if document.name:
            return f"{document.name}{suffix}"
        hash_source = content or document.content
        if hash_source:
            content_hash = hashlib.md5(hash_source.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
            return f"chunk_{content_hash}{suffix}"
        return None

    def clean_text(self, text: str) -> str:
        """Collapse repeated runs of each whitespace character while preserving line breaks."""
        for pattern, replacement in (
            (r"\n+", "\n"),
            (r" +", " "),
            (r"\t+", "\t"),
            (r"\r+", "\r"),
            (r"\f+", "\f"),
            (r"\v+", "\v"),
        ):
            text = re.sub(pattern, replacement, text)
        return text


class FixedSizeChunking(ChunkingStrategy):
    """Split text into fixed-size character chunks with optional overlap."""

    def __init__(self, chunk_size: int = 5000, overlap: int = 0) -> None:
        if overlap >= chunk_size:
            msg = f"Invalid parameters: overlap ({overlap}) must be less than chunk size ({chunk_size})."
            raise ValueError(msg)
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk(self, document: Document) -> list[Document]:
        """Split ``document`` into fixed-size chunks aligned to whitespace when possible."""
        content = self.clean_text(document.content)
        content_length = len(content)
        chunked: list[Document] = []
        chunk_number = 1
        start = 0

        while start < content_length:
            end = min(start + self.chunk_size, content_length)
            if end < content_length:
                while end > start and content[end] not in (" ", "\n", "\r", "\t"):
                    end -= 1
            if end == start:
                end = start + self.chunk_size

            chunk = content[start:end]
            meta_data = {**document.meta_data, "chunk": chunk_number, "chunk_size": len(chunk)}
            chunked.append(
                Document(
                    id=self._generate_chunk_id(document, chunk_number, chunk),
                    name=document.name,
                    meta_data=meta_data,
                    content=chunk,
                ),
            )
            chunk_number += 1
            if end >= content_length:
                break
            start = max(start + 1, end - self.overlap)

        return chunked


class DocumentChunking(ChunkingStrategy):
    """Split text on paragraph boundaries, falling back to sentences for oversized paragraphs."""

    def __init__(self, chunk_size: int = 5000, overlap: int = 0) -> None:
        if overlap >= chunk_size:
            msg = f"Invalid parameters: overlap ({overlap}) must be less than chunk size ({chunk_size})."
            raise ValueError(msg)
        self.chunk_size = chunk_size
        self.overlap = overlap

    def _make_chunk(self, document: Document, chunk_number: int, content: str) -> Document:
        meta_data = {**document.meta_data, "chunk": chunk_number, "chunk_size": len(content)}
        return Document(
            id=self._generate_chunk_id(document, chunk_number, content),
            name=document.name,
            meta_data=meta_data,
            content=content,
        )

    def _apply_overlap(self, document: Document, chunks: list[Document]) -> list[Document]:
        overlapped: list[Document] = []
        for index, chunk_doc in enumerate(chunks):
            if index > 0 and (prev_text := chunks[index - 1].content[-self.overlap :]):
                overlapped.append(
                    self._make_chunk(document, chunk_doc.meta_data["chunk"], prev_text + chunk_doc.content),
                )
            else:
                overlapped.append(chunk_doc)
        return overlapped

    def _accumulate_sentences(
        self,
        document: Document,
        para: str,
        chunks: list[Document],
    ) -> tuple[list[str], int]:
        current: list[str] = []
        current_size = 0
        for raw_sentence in _SENTENCE_SPLIT_RE.split(para):
            sentence = raw_sentence.strip()
            if not sentence:
                continue
            sentence_size = len(sentence)
            if current_size + sentence_size <= self.chunk_size:
                current.append(sentence)
                current_size += sentence_size
            else:
                if current:
                    chunks.append(self._make_chunk(document, len(chunks) + 1, " ".join(current)))
                current = [sentence]
                current_size = sentence_size
        return current, current_size

    def chunk(self, document: Document) -> list[Document]:
        """Split ``document`` by paragraphs, then sentences, with optional trailing overlap."""
        if len(document.content) <= self.chunk_size:
            return [document]

        chunks: list[Document] = []
        current: list[str] = []
        current_size = 0

        for raw_para in document.content.split("\n\n"):
            para = self.clean_text(raw_para).strip()
            if not para:
                continue
            para_size = len(para)
            if para_size > self.chunk_size:
                if current:
                    chunks.append(self._make_chunk(document, len(chunks) + 1, "\n\n".join(current)))
                current, current_size = self._accumulate_sentences(document, para, chunks)
            elif current_size + para_size <= self.chunk_size:
                current.append(para)
                current_size += para_size
            else:
                if current:
                    chunks.append(self._make_chunk(document, len(chunks) + 1, "\n\n".join(current)))
                current = [para]
                current_size = para_size

        if current:
            chunks.append(self._make_chunk(document, len(chunks) + 1, "\n\n".join(current)))
        return self._apply_overlap(document, chunks) if self.overlap > 0 else chunks


class RowChunking(ChunkingStrategy):
    """Split line-delimited tabular text into one chunk per non-empty row."""

    def __init__(self, skip_header: bool = False, clean_rows: bool = True) -> None:
        self.skip_header = skip_header
        self.clean_rows = clean_rows

    def chunk(self, document: Document) -> list[Document]:
        """Split ``document`` into one chunk per non-empty line, keeping logical 1-based row numbers."""
        if not document or not document.content:
            return []

        rows = document.content.splitlines()
        start_index = document.meta_data.get("start_row", 1)
        if self.skip_header and rows and start_index == 1:
            rows = rows[1:]
            start_index += 1

        chunks: list[Document] = []
        for offset, row in enumerate(rows):
            chunk_content = " ".join(row.split()) if self.clean_rows else row.strip()
            if not chunk_content:
                continue
            row_number = start_index + offset
            meta_data = {**document.meta_data, "row_number": row_number}
            chunks.append(
                Document(
                    id=self._generate_chunk_id(document, row_number, chunk_content, prefix="row"),
                    name=document.name,
                    meta_data=meta_data,
                    content=chunk_content,
                ),
            )
        return chunks


class SafeFixedSizeChunking(FixedSizeChunking):
    """Avoid pathological micro-chunks when whitespace is far from the boundary."""

    def __init__(
        self,
        chunk_size: int = 5000,
        overlap: int = 0,
        *,
        min_chunk_fill_ratio: float = 0.5,
    ) -> None:
        super().__init__(chunk_size=chunk_size, overlap=overlap)
        if not 0 < min_chunk_fill_ratio <= 1:
            msg = "min_chunk_fill_ratio must be in the range (0, 1]"
            raise ValueError(msg)
        self.min_chunk_fill_ratio = min_chunk_fill_ratio

    def clean_text(self, text: str) -> str:
        """Clean text as every indexed chunk has been cleaned (``_legacy_clean_text``)."""
        return _legacy_clean_text(text)

    def _min_chunk_size(self) -> int:
        """Return the shortest chunk a whitespace-aligned split may produce."""
        return max(1, int(self.chunk_size * self.min_chunk_fill_ratio))

    def chunk(self, document: Document) -> list[Document]:
        """Split one document while avoiding tiny boundary fragments."""
        content = self.clean_text(document.content)
        content_length = len(content)
        chunked_documents: list[Document] = []
        chunk_number = 1
        min_chunk_size = self._min_chunk_size()
        start = 0

        while start < content_length:
            raw_end = min(start + self.chunk_size, content_length)
            end = raw_end

            if raw_end < content_length:
                while end > start and content[end] not in [" ", "\n", "\r", "\t"]:
                    end -= 1

                # Prefer a hard split over tiny overlap-driven fragments when the
                # nearest whitespace is too far from the target boundary.
                if end == start or (end - start) < min_chunk_size:
                    end = raw_end

            chunk = content[start:end]
            meta_data = (document.meta_data or {}).copy()
            meta_data["chunk"] = chunk_number
            meta_data["chunk_size"] = len(chunk)
            chunked_documents.append(
                Document(
                    id=self._generate_chunk_id(document, chunk_number, chunk),
                    name=document.name,
                    meta_data=meta_data,
                    content=chunk,
                ),
            )

            if end >= content_length:
                break

            next_start = end - self.overlap
            if next_start <= start:
                next_start = end
            start = next_start
            chunk_number += 1

        return chunked_documents


class LegacyFixedSizeChunking(FixedSizeChunking):
    """Fixed-size chunking for JSON files, cleaning text as every indexed chunk has been."""

    def clean_text(self, text: str) -> str:
        """Clean the document with ``_legacy_clean_text``."""
        return _legacy_clean_text(text)


class LegacyDocumentChunking(DocumentChunking):
    """Paragraph chunking for PDF, DOCX, and PPTX files, cleaning paragraphs as every indexed chunk has been."""

    def clean_text(self, text: str) -> str:
        """Clean one paragraph with ``_legacy_clean_text``."""
        return _legacy_clean_text(text)


__all__ = [
    "ChunkingStrategy",
    "Document",
    "DocumentChunking",
    "FixedSizeChunking",
    "LegacyDocumentChunking",
    "LegacyFixedSizeChunking",
    "RowChunking",
    "SafeFixedSizeChunking",
]
