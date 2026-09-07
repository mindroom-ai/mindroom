"""Tests for strict knowledge error propagation."""

from __future__ import annotations

from typing import Any

import pytest
from agno.knowledge.content import Content
from agno.knowledge.document import Document

from mindroom.strict_knowledge import StrictInsertKnowledge, StrictSearchKnowledge


class _FailingVectorDb:
    def exists(self) -> bool:
        return True

    def upsert_available(self) -> bool:
        return False

    def insert(
        self,
        _content_hash: str,
        *,
        documents: list[Document],
        filters: dict[str, Any] | None,
    ) -> None:
        del documents, filters
        msg = "vector insertion failed"
        raise RuntimeError(msg)


def test_strict_insert_knowledge_propagates_vector_failure() -> None:
    """Index callers receive the causal failure instead of a vectorless success."""
    knowledge = StrictInsertKnowledge(vector_db=_FailingVectorDb())
    content = Content(content_hash="hash")

    with pytest.raises(RuntimeError, match="vector insertion failed"):
        knowledge._handle_vector_db_insert(content, [Document(content="text")], upsert=False)


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


class _UnembeddingVectorDb:
    """A store that embeds in-process but leaves every chunk without a vector."""

    embedder = object()

    def exists(self) -> bool:
        return True

    def upsert_available(self) -> bool:
        return False

    def insert(self, _content_hash: str, *, documents: list[Document], filters: object) -> None:
        del documents, filters


def test_strict_insert_knowledge_raises_when_chunks_were_not_embedded() -> None:
    """A write the store accepted without embedding anything must not read as success."""
    knowledge = StrictInsertKnowledge(vector_db=_UnembeddingVectorDb())
    content = Content(content_hash="hash")

    with pytest.raises(RuntimeError, match="No chunks could be embedded"):
        knowledge._handle_vector_db_insert(content, [Document(content="text")], upsert=False)
