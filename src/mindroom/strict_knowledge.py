"""Knowledge handle whose searches propagate failures instead of returning [].

agno's ``Knowledge.search``/``asearch`` (verified against agno 2.6.12) wrap the
vector-db call in ``except Exception``, log the raw exception text, and return
an empty list — turning an embedder credential failure into fake-empty search
results, the exact silent degradation ISSUE-237 exists to kill. Every MindRoom
read handle uses this subclass so provider failures propagate to the caller:
agno's ``search_knowledge_base`` tool boundary then reports a visible error
(exception type name only), and MindRoom's own callers classify the failure.
MindRoom never sets ``isolate_vector_search`` or per-call search types, so the
overrides skip agno's filter-injection branch; revisit on agno upgrades.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.knowledge.knowledge import Knowledge

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
    ) -> list[Document]:
        """Return matching documents; raise on vector-db or embedder failure."""
        del search_type  # MindRoom read handles never override per-call search types.
        if self.vector_db is None:
            return []
        return self.vector_db.search(query=query, limit=max_results or self.max_results, filters=filters)

    async def asearch(
        self,
        query: str,
        max_results: int | None = None,
        filters: dict[str, Any] | list[Any] | None = None,
        search_type: str | None = None,
    ) -> list[Document]:
        """Async variant of ``search`` with agno's sync fallback preserved."""
        del search_type
        if self.vector_db is None:
            return []
        limit = max_results or self.max_results
        try:
            return await self.vector_db.async_search(query=query, limit=limit, filters=filters)
        except NotImplementedError:
            return self.vector_db.search(query=query, limit=limit, filters=filters)
