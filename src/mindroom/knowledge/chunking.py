"""Native chunk data model and stateless chunking functions for knowledge indexing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from mindroom.knowledge.memory_entries import split_memory_entries

if TYPE_CHECKING:
    from collections.abc import Mapping

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_EMPTY_METADATA: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class KnowledgeChunk:
    """One chunk produced for knowledge indexing, before it is embedded and stored."""

    text: str
    #: The source file's basename, which search results report as the document name.
    name: str | None = None
    #: What the reader and chunker recorded, such as ``chunk``, ``chunk_size``, ``page``, or ``row_number``.
    metadata: Mapping[str, Any] = field(default_factory=dict)


def collapse_whitespace(text: str) -> str:
    """Collapse each whitespace run, line breaks included, to one space."""
    return re.sub(r"\s+", " ", text)


def _validate_fixed_params(chunk_size: int, overlap: int, min_chunk_fill_ratio: float | None) -> None:
    if chunk_size <= 0:
        msg = f"Invalid parameters: chunk_size ({chunk_size}) must be positive."
        raise ValueError(msg)
    if overlap < 0 or overlap >= chunk_size:
        msg = f"Invalid parameters: overlap ({overlap}) must be in [0, {chunk_size})."
        raise ValueError(msg)
    if min_chunk_fill_ratio is not None and not 0 < min_chunk_fill_ratio <= 1:
        msg = "min_chunk_fill_ratio must be in the range (0, 1]"
        raise ValueError(msg)


def _numbered_chunk(text: str, number: int, name: str | None, metadata: Mapping[str, Any]) -> KnowledgeChunk:
    return KnowledgeChunk(text=text, name=name, metadata={**metadata, "chunk": number, "chunk_size": len(text)})


def chunk_fixed(
    text: str,
    *,
    name: str | None = None,
    metadata: Mapping[str, Any] = _EMPTY_METADATA,
    chunk_size: int = 5000,
    overlap: int = 0,
    min_chunk_fill_ratio: float | None = 0.5,
) -> list[KnowledgeChunk]:
    """Split text into fixed-size character chunks aligned to whitespace when possible."""
    _validate_fixed_params(chunk_size, overlap, min_chunk_fill_ratio)
    content = collapse_whitespace(text)
    content_length = len(content)
    min_chunk_size = max(1, int(chunk_size * min_chunk_fill_ratio)) if min_chunk_fill_ratio is not None else 0
    chunks: list[KnowledgeChunk] = []
    start = 0

    while start < content_length:
        raw_end = min(start + chunk_size, content_length)
        end = raw_end
        if raw_end < content_length:
            while end > start and content[end] not in (" ", "\n", "\r", "\t"):
                end -= 1
            if end == start or (min_chunk_fill_ratio is not None and (end - start) < min_chunk_size):
                end = raw_end

        chunks.append(_numbered_chunk(content[start:end], len(chunks) + 1, name, metadata))
        if end >= content_length:
            break

        if min_chunk_fill_ratio is None:
            start = max(start + 1, end - overlap)
        else:
            next_start = end - overlap
            start = end if next_start <= start else next_start

    return chunks


def _accumulate_sentences(
    para: str,
    chunks: list[KnowledgeChunk],
    name: str | None,
    metadata: Mapping[str, Any],
    chunk_size: int,
) -> tuple[list[str], int]:
    current: list[str] = []
    current_size = 0
    for raw_sentence in _SENTENCE_SPLIT_RE.split(para):
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        sentence_size = len(sentence)
        if current_size + sentence_size <= chunk_size:
            current.append(sentence)
            current_size += sentence_size
        else:
            if current:
                chunks.append(_numbered_chunk(" ".join(current), len(chunks) + 1, name, metadata))
            current = [sentence]
            current_size = sentence_size
    return current, current_size


def chunk_paragraphs(
    text: str,
    *,
    name: str | None = None,
    metadata: Mapping[str, Any] = _EMPTY_METADATA,
    chunk_size: int = 5000,
) -> list[KnowledgeChunk]:
    """Split text on paragraph boundaries, falling back to sentences for oversized paragraphs."""
    if chunk_size <= 0:
        msg = f"Invalid parameters: chunk_size ({chunk_size}) must be positive."
        raise ValueError(msg)
    if len(text) <= chunk_size:
        return [KnowledgeChunk(text=text, name=name, metadata=dict(metadata))]

    chunks: list[KnowledgeChunk] = []
    current: list[str] = []
    current_size = 0

    for raw_para in text.split("\n\n"):
        para = collapse_whitespace(raw_para).strip()
        if not para:
            continue
        para_size = len(para)
        if para_size > chunk_size:
            if current:
                chunks.append(_numbered_chunk("\n\n".join(current), len(chunks) + 1, name, metadata))
            current, current_size = _accumulate_sentences(para, chunks, name, metadata, chunk_size)
        elif current_size + para_size <= chunk_size:
            current.append(para)
            current_size += para_size
        else:
            if current:
                chunks.append(_numbered_chunk("\n\n".join(current), len(chunks) + 1, name, metadata))
            current = [para]
            current_size = para_size

    if current:
        chunks.append(_numbered_chunk("\n\n".join(current), len(chunks) + 1, name, metadata))
    return chunks


def chunk_rows(
    text: str,
    *,
    name: str | None = None,
    metadata: Mapping[str, Any] = _EMPTY_METADATA,
) -> list[KnowledgeChunk]:
    """Split line-delimited tabular text into one chunk per non-empty row."""
    if not text:
        return []
    return [
        KnowledgeChunk(text=chunk_content, name=name, metadata={**metadata, "row_number": row_number})
        for row_number, row in enumerate(text.splitlines(), start=1)
        if (chunk_content := " ".join(row.split()))
    ]


def chunk_memory_entries(
    text: str,
    *,
    name: str | None = None,
    metadata: Mapping[str, Any] = _EMPTY_METADATA,
    chunk_size: int = 5000,
    overlap: int = 0,
) -> list[KnowledgeChunk]:
    """Split a memory file into one chunk per entry, falling back to fixed-size chunking when empty."""
    _validate_fixed_params(chunk_size, overlap, 0.5)
    entries = split_memory_entries(text, chunk_size)
    if not entries:
        return chunk_fixed(text, name=name, metadata=metadata, chunk_size=chunk_size, overlap=overlap)
    return [
        _numbered_chunk(collapse_whitespace(entry), number, name, metadata)
        for number, entry in enumerate(entries, start=1)
    ]


def chunk_text(
    text: str,
    *,
    name: str | None = None,
    metadata: Mapping[str, Any] = _EMPTY_METADATA,
    chunk_size: int = 5000,
    overlap: int = 0,
    strategy: Literal["fixed", "memory_entries"] = "fixed",
) -> list[KnowledgeChunk]:
    """Chunk plain text using the configured fixed-size or memory-entry strategy."""
    if strategy == "memory_entries":
        return chunk_memory_entries(text, name=name, metadata=metadata, chunk_size=chunk_size, overlap=overlap)
    return chunk_fixed(text, name=name, metadata=metadata, chunk_size=chunk_size, overlap=overlap)


__all__ = [
    "KnowledgeChunk",
    "chunk_fixed",
    "chunk_memory_entries",
    "chunk_paragraphs",
    "chunk_rows",
    "chunk_text",
    "collapse_whitespace",
]
