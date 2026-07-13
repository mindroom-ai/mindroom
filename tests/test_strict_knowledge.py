"""Tests for strict knowledge error propagation."""

from __future__ import annotations

from typing import Any

import pytest
from agno.knowledge.content import Content
from agno.knowledge.document import Document

from mindroom.strict_knowledge import StrictInsertKnowledge


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
