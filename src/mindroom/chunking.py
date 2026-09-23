"""MindRoom-specific chunking helpers."""

from __future__ import annotations

from agno.knowledge.chunking.fixed import FixedSizeChunking
from agno.knowledge.document.base import Document

#: Largest number of UTF-8 bytes a single character can occupy.
_MAX_UTF8_BYTES_PER_CHARACTER = 4


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

    def _min_chunk_size(self) -> int:
        """Return the shortest chunk a whitespace-aligned split may produce."""
        return max(1, int(self.chunk_size * self.min_chunk_fill_ratio))

    def _min_chunk_step(self) -> int:
        """Return the smallest distance between two consecutive chunk starts.

        A chunk that keeps its whitespace boundary is at least
        ``_min_chunk_size`` characters long, and the next chunk starts
        ``overlap`` characters before its end, so a start advances by at least
        ``min_chunk_size - overlap``. Once the overlap reaches that length the
        advance can shrink to one character, which is what makes near-total
        overlap so expensive.
        """
        return max(1, self._min_chunk_size() - self.overlap)

    def max_chunk_text_bytes(self, source_bytes: int) -> int:
        """Return an upper bound on the UTF-8 bytes :meth:`chunk` emits for one file.

        ``source_bytes`` is a file's size on disk. Readers decode files as
        UTF-8, so the decoded content holds at most that many characters and at
        most that many UTF-8 bytes, and ``clean_text`` only collapses
        whitespace runs, so neither count grows before chunking. Two
        independent bounds hold on top of that, and the tighter one wins:

        * no character is covered by more than ``chunk_size / min_step`` chunks,
          because chunks are at most ``chunk_size`` long and their starts are at
          least ``min_step`` apart;
        * every chunk but the first repeats at most ``overlap`` characters of
          its predecessor, and a character costs at most four UTF-8 bytes.
        """
        if source_bytes <= 0:
            return 0
        if self.overlap <= 0:
            # Chunks partition content that cleaning only ever shrank.
            return source_bytes
        step = self._min_chunk_step()
        max_chunks = (source_bytes - 1) // step + 1
        by_repeats = source_bytes + max_chunks * self.overlap * _MAX_UTF8_BYTES_PER_CHARACTER
        by_coverage = ((self.chunk_size - 1) // step + 1) * source_bytes
        return min(by_repeats, by_coverage)

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
