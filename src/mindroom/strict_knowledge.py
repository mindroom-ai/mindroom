"""Knowledge handle whose searches propagate failures instead of returning [].

agno's ``Knowledge.search``/``asearch`` (verified against agno 3.0.5) wrap the
vector-db call in ``except Exception``, log the exception text, and return an
empty list — turning an embedder credential failure into fake-empty search
results, the exact silent degradation ISSUE-237 exists to kill. Every MindRoom
read handle uses this subclass so provider failures propagate to the caller:
agno's ``search_knowledge_base`` tool boundary then reports a visible error
(exception type name only), and MindRoom's own callers classify the failure.

MindRoom knowledge is shared, never per-user: the ``user_id`` agno forwards from
the run context is dropped so a scoped search still reads the shared index, and
``isolate_vector_search`` is never set, so the overrides skip agno's
filter-injection branch. Revisit on agno upgrades.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.knowledge.content import Content, ContentStatus
from agno.knowledge.knowledge import Knowledge
from agno.knowledge.utils import set_agno_metadata

if TYPE_CHECKING:
    from agno.knowledge.document import Document


@dataclass
class StrictSearchKnowledge(Knowledge):
    """Knowledge whose search paths raise on vector-db or embedder failure."""

    def search(
        self,
        query: str,
        max_results: int | None = None,
        filters: dict[str, Any] | list[Any] | None = None,
        search_type: str | None = None,
        user_id: str | None = None,
    ) -> list[Document]:
        """Return matching documents; raise on vector-db or embedder failure."""
        del search_type, user_id  # MindRoom read handles are shared and never override per-call search types.
        if self.vector_db is None:
            return []
        return self.vector_db.search(query=query, limit=max_results or self.max_results, filters=filters)

    async def asearch(
        self,
        query: str,
        max_results: int | None = None,
        filters: dict[str, Any] | list[Any] | None = None,
        search_type: str | None = None,
        user_id: str | None = None,
    ) -> list[Document]:
        """Async variant of ``search`` with agno's sync fallback preserved."""
        del search_type, user_id
        if self.vector_db is None:
            return []
        limit = max_results or self.max_results
        try:
            return await self.vector_db.async_search(query=query, limit=limit, filters=filters)
        except NotImplementedError:
            return self.vector_db.search(query=query, limit=limit, filters=filters)


@dataclass
class StrictInsertKnowledge(Knowledge):
    """Knowledge whose vector insertion path propagates failures."""

    def _handle_vector_db_insert(
        self,
        content: Content,
        read_documents: list[Document],
        upsert: bool,
        prior_status: ContentStatus | None = None,
    ) -> None:
        """Mirror Agno's insertion path without its catch-log-and-return behavior.

        MindRoom rebuilds candidate collections from scratch, so the re-ingest
        branch Agno keys off ``prior_status`` never applies here.
        """
        del prior_status
        if self.vector_db is None:
            msg = "No vector database configured"
            raise RuntimeError(msg)
        if self.vector_db.upsert_available() and upsert:
            self.vector_db.upsert(content.content_hash, read_documents, content.metadata)
        else:
            self.vector_db.insert(content.content_hash, documents=read_documents, filters=content.metadata)
        content.metadata = set_agno_metadata(content.metadata, "vectors_indexed", True)
        self._set_embedding_success_status(content, read_documents)
        if content.status is not ContentStatus.COMPLETED:
            # Agno downgrades to PARTIAL/FAILED when chunks silently lack an embedding.
            msg = content.status_message or f"Knowledge content {content.id or content.name!r} was not fully embedded"
            raise RuntimeError(msg)
        self._update_content(content)
