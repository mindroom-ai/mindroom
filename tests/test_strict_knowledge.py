"""Tests for the Agno knowledge handle that delegates search to MindRoom's own knowledge search."""

from __future__ import annotations

import pytest

from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.knowledge.strict_knowledge import StrictSearchKnowledge


class _RecordingIndex:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        self.calls.append((query, limit))
        return [SearchHit(content="hit", name="notes.md", meta_data={"source_path": "notes.md"})]

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        return self.search(query, limit=limit)


class _FailingIndex(_RecordingIndex):
    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        del query, limit
        msg = "vector search failed"
        raise RuntimeError(msg)


def _handle(index: _RecordingIndex) -> StrictSearchKnowledge:
    source = KnowledgeSource(description=KnowledgeSourceDescription(base_id="docs", description=""), index=index)
    return StrictSearchKnowledge(AgentKnowledge(sources=(source,)))


def test_strict_search_knowledge_drops_agno_user_scope() -> None:
    """Agno forwards the run's user id; MindRoom knowledge is shared, so the search stays unscoped."""
    index = _RecordingIndex()
    knowledge = _handle(index)

    documents = knowledge.search("query", user_id="@alice:example.test")

    assert [(document.name, document.meta_data, document.content) for document in documents] == [
        ("notes.md", {"source_path": "notes.md"}, "hit"),
    ]
    assert index.calls == [("query", knowledge.max_results)]


def test_strict_search_knowledge_propagates_search_failure() -> None:
    """Failed searches must not become plausible empty search results."""
    knowledge = _handle(_FailingIndex())
    with pytest.raises(RuntimeError, match="vector search failed"):
        knowledge.search("query")


@pytest.mark.asyncio
async def test_strict_search_knowledge_propagates_async_search_failure() -> None:
    """The async search path retains the index's failure."""
    knowledge = _handle(_FailingIndex())
    with pytest.raises(RuntimeError, match="vector search failed"):
        await knowledge.asearch("query")


def test_strict_search_knowledge_prompt_block_is_the_owned_one() -> None:
    """Agno renders the handle's prompt block from MindRoom's own knowledge search."""
    knowledge = _handle(_RecordingIndex())

    assert knowledge.build_context(enable_agentic_filters=False, user_id="@alice:example.test") == (
        knowledge.agent_knowledge.prompt_block()
    )
