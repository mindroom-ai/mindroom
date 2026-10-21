"""Tests for strict knowledge search error propagation."""

from __future__ import annotations

import pytest
from agno.knowledge.document import Document

from mindroom.knowledge.strict_knowledge import StrictSearchKnowledge


class _RecordingVectorDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, object]] = []

    def exists(self) -> bool:
        return True

    def search(self, *, query: str, limit: int, filters: object = None) -> list[Document]:
        self.calls.append((query, limit, filters))
        return [Document(content="hit")]


def test_strict_search_knowledge_drops_agno_user_scope() -> None:
    """Agno forwards the run's user id; MindRoom knowledge is shared, so the search stays unscoped."""
    vector_db = _RecordingVectorDb()
    knowledge = StrictSearchKnowledge(vector_db=vector_db, max_results=3)

    documents = knowledge.search("query", user_id="@alice:example.test")

    assert [document.content for document in documents] == ["hit"]
    assert vector_db.calls == [("query", 3, None)]


class _FailingSearchVectorDb(_RecordingVectorDb):
    def search(self, *, query: str, limit: int, filters: object = None) -> list[Document]:
        del query, limit, filters
        msg = "vector search failed"
        raise RuntimeError(msg)

    async def async_search(self, *, query: str, limit: int, filters: object = None) -> list[Document]:
        return self.search(query=query, limit=limit, filters=filters)


def test_strict_search_knowledge_propagates_vector_failure() -> None:
    """Failed searches must not become plausible empty search results."""
    knowledge = StrictSearchKnowledge(vector_db=_FailingSearchVectorDb())
    with pytest.raises(RuntimeError, match="vector search failed"):
        knowledge.search("query")


@pytest.mark.asyncio
async def test_strict_search_knowledge_propagates_async_vector_failure() -> None:
    """The async search path retains the vector provider's failure."""
    knowledge = StrictSearchKnowledge(vector_db=_FailingSearchVectorDb())
    with pytest.raises(RuntimeError, match="vector search failed"):
        await knowledge.asearch("query")


@pytest.mark.asyncio
async def test_strict_search_knowledge_sync_fallback_propagates_failure() -> None:
    """An unimplemented async adapter must not hide a failed synchronous fallback."""

    class SyncOnlyVectorDb(_FailingSearchVectorDb):
        async def async_search(self, *, query: str, limit: int, filters: object = None) -> list[Document]:
            raise NotImplementedError

    knowledge = StrictSearchKnowledge(vector_db=SyncOnlyVectorDb())
    with pytest.raises(RuntimeError, match="vector search failed"):
        await knowledge.asearch("query")
