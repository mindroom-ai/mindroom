"""Published knowledge index handle: embeds queries with the configured embedder and searches the store in process."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.knowledge.collection_lifetime import read_collection
from mindroom.knowledge.vector_store import KnowledgeCollection

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.embeddings.base import Embedder
    from mindroom.knowledge.indexing_config import IndexingSettings
    from mindroom.knowledge.read_protocol import SearchHit


@dataclass
class PublishedIndexReader:
    """A scoped read handle; each read selects and holds the current compatible publication."""

    collection_name: str
    path: str
    embedder: Embedder
    published_settings: IndexingSettings | None = None

    def _published_metadata(self) -> dict[str, str] | None:
        """Return the settings that select this handle's compatible publication, or None to read its collection."""
        return self.published_settings.to_metadata() if self.published_settings is not None else None

    def exists(self) -> bool:
        """Probe the same compatible publication that a subsequent search resolves."""
        with read_collection(self.path, self.collection_name, self._published_metadata()) as collection_name:
            return KnowledgeCollection(path=Path(self.path), name=collection_name).exists()

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        """Embed under the caller's credentials, then search on a worker thread."""
        embedding = (await self.embedder.aembed([query], purpose="query"))[0]
        return await asyncio.to_thread(self._search, embedding, limit)

    def _search(self, embedding: Sequence[float], limit: int) -> list[SearchHit]:
        with read_collection(self.path, self.collection_name, self._published_metadata()) as collection_name:
            return KnowledgeCollection(path=Path(self.path), name=collection_name).search(embedding, limit=limit)
