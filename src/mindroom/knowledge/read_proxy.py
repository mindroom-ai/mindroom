"""Published knowledge handle with parent embeddings and isolated native reads."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from agno.knowledge.document import Document

from mindroom.knowledge.read_process import read_chroma, read_chroma_async
from mindroom.knowledge.read_protocol import ReadRequest
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder

logger = get_logger(__name__)


def collection_exists(path: str, collection: str) -> bool:
    """Probe native metadata in the isolated reader process."""
    return read_chroma(ReadRequest(path=path, collection=collection)).exists


def _dict_filters(filters: dict[str, Any] | list[Any] | None) -> dict[str, Any] | None:
    if isinstance(filters, list):
        logger.warning("Filter expressions are not supported by Chroma; no filters applied")
        return None
    return filters


@dataclass
class ChromaReadProxy:
    """A typed read-only descriptor; all native handles belong to the worker."""

    collection_name: str
    path: str
    embedder: Embedder

    def exists(self) -> bool:
        """Check whether the exact published collection still exists."""
        return collection_exists(self.path, self.collection_name)

    def create(self) -> None:
        """Never recreate a vanished published collection during Agno initialization."""
        message = "Published knowledge collection is unavailable"
        raise RuntimeError(message)

    def search(
        self,
        query: str,
        limit: int = 5,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        """Embed under the caller's credentials, then query outside this process."""
        embedding = self.embedder.get_embedding(query)
        result = read_chroma(
            ReadRequest(self.path, self.collection_name, query, embedding, limit, _dict_filters(filters)),
        )
        return [Document(**asdict(document)) for document in result.documents]

    async def async_search(
        self,
        query: str,
        limit: int = 5,
        filters: dict[str, Any] | list[Any] | None = None,
    ) -> list[Document]:
        """Start native imports while the parent obtains the query embedding."""

        async def prepare_request() -> ReadRequest:
            try:
                embedding = await self.embedder.async_get_embedding(query)
            except NotImplementedError:
                embedding = await asyncio.to_thread(self.embedder.get_embedding, query)
            return ReadRequest(self.path, self.collection_name, query, embedding, limit, _dict_filters(filters))

        result = await read_chroma_async(prepare_request)
        return [Document(**asdict(document)) for document in result.documents]
