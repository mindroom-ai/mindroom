"""Agno knowledge adapters for owner-controlled search failures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agno.knowledge.document import Document
    from agno.knowledge.knowledge import Knowledge


# AGNO_COMPAT: Knowledge search hides provider failures as empty results.
# Reason: Knowledge.search/asearch catch vector/provider errors and return [],
# preventing callers from distinguishing an empty index from a failed search.
# Upstream issue: https://github.com/agno-agi/agno/issues/10150
# Upstream PR: https://github.com/agno-agi/agno/pull/10152 (search errors only).
# Remove when: The pinned public search API propagates errors on request; retain
# the owner's shared-index scope and default limit policy.
# Coverage: tests/test_strict_knowledge.py::test_strict_search_knowledge_propagates_vector_failure;
# tests/test_strict_knowledge.py::test_strict_search_knowledge_propagates_async_vector_failure;
# tests/test_strict_knowledge.py::test_strict_search_knowledge_sync_fallback_propagates_failure.
def search(
    knowledge: Knowledge,
    query: str,
    *,
    limit: int,
    filters: dict[str, Any] | list[Any] | None,
) -> list[Document]:
    """Search the vector adapter without Agno's catch-and-empty error policy."""
    if knowledge.vector_db is None:
        return []
    return knowledge.vector_db.search(query=query, limit=limit, filters=filters)


async def asearch(
    knowledge: Knowledge,
    query: str,
    *,
    limit: int,
    filters: dict[str, Any] | list[Any] | None,
) -> list[Document]:
    """Retain Agno's sync fallback while letting either adapter propagate errors."""
    if knowledge.vector_db is None:
        return []
    try:
        return await knowledge.vector_db.async_search(query=query, limit=limit, filters=filters)
    except NotImplementedError:
        return knowledge.vector_db.search(query=query, limit=limit, filters=filters)
