"""MindRoom-specific chunking helpers."""

from __future__ import annotations

from agno.knowledge.chunking.fixed import FixedSizeChunking
from agno.knowledge.document.base import Document


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

    def chunk(self, document: Document) -> list[Document]:
        content = self.clean_text(document.content)
        content_length = len(content)
        chunked_documents: list[Document] = []
        chunk_number = 1
        min_chunk_size = max(1, int(self.chunk_size * self.min_chunk_fill_ratio))
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
                )
            )

            if end >= content_length:
                break

            next_start = end - self.overlap
            if next_start <= start:
                next_start = end
            start = next_start
            chunk_number += 1

        return chunked_documents
