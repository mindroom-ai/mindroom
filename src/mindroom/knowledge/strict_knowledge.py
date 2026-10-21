"""Knowledge handle whose searches propagate failures instead of returning [].

agno's ``Knowledge.search``/``asearch`` (verified against agno 3.0.9) wrap the
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

from agno.knowledge.knowledge import Knowledge

from mindroom.knowledge import agno_compat_knowledge

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
        return agno_compat_knowledge.search(self, query, limit=max_results or self.max_results, filters=filters)

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
        return await agno_compat_knowledge.asearch(self, query, limit=max_results or self.max_results, filters=filters)
