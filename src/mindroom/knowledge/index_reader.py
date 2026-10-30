"""Published knowledge index handle: embeds queries with the configured embedder and searches the store in process."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
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

#: A search's whole budget: embedding the query, waiting for the collection, and ranking it.
_SEARCH_TIMEOUT_SECONDS = 30.0
#: Searches wait on collection locks and load matrices here, never in the event loop's shared default executor.
_search_threads = ThreadPoolExecutor(4, thread_name_prefix="knowledge-search")


@dataclass
class PublishedIndexReader:
    """A scoped read handle; each read selects and holds the current publication compatible with ``settings``."""

    path: str
    embedder: Embedder
    settings: IndexingSettings
    #: The most bytes of vectors a search may load, or None for no limit.
    max_matrix_bytes: int | None = None

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        """Embed under the caller's credentials, then run the hybrid search on a search thread, all within a deadline."""
        async with asyncio.timeout(_SEARCH_TIMEOUT_SECONDS):
            embedding = (await self.embedder.aembed([query], purpose="query"))[0]
            return await asyncio.get_running_loop().run_in_executor(
                _search_threads,
                self._search,
                query,
                embedding,
                limit,
            )

    def _search(self, query: str, embedding: Sequence[float], limit: int) -> list[SearchHit]:
        with read_collection(Path(self.path), self.settings) as collection_name:
            return KnowledgeCollection(path=Path(self.path), name=collection_name).search(
                embedding,
                limit=limit,
                query=query,
                max_matrix_bytes=self.max_matrix_bytes,
            )
