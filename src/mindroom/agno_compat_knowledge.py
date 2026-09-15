"""Agno knowledge adapters for owner-controlled search and insertion failures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.knowledge.utils import set_agno_metadata

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.knowledge.content import Content
    from agno.knowledge.document import Document
    from agno.knowledge.knowledge import Knowledge


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


# Reason: Knowledge's private insertion handler catches vector failures and owns
# status updates without a public error/validation callback for the caller.
# Upstream issue: No matching public insertion error-policy issue identified.
# Upstream PR: https://github.com/agno-agi/agno/pull/9814 improves embedding statuses,
# but does not supply an insertion propagation/validation hook; #10152 is search-only.
# Remove when: Public insertion APIs propagate failures and allow owner validation
# before publishing content status. Retain full-embedding and candidate-rebuild policy.
# Coverage: tests/test_strict_knowledge.py::test_strict_insert_knowledge_propagates_vector_failure;
# tests/test_strict_knowledge.py::test_strict_insert_knowledge_raises_when_chunks_were_not_embedded;
# tests/test_knowledge_manager.py.
def insert_documents(
    knowledge: Knowledge,
    content: Content,
    documents: list[Document],
    *,
    upsert: bool,
    validate: Callable[[Content], None],
) -> None:
    """Run Agno's vector/status plumbing with failures visible to the owner."""
    if knowledge.vector_db is None:
        msg = "No vector database configured"
        raise RuntimeError(msg)
    if knowledge.vector_db.upsert_available() and upsert:
        knowledge.vector_db.upsert(content.content_hash, documents, content.metadata)
    else:
        knowledge.vector_db.insert(content.content_hash, documents=documents, filters=content.metadata)
    content.metadata = set_agno_metadata(content.metadata, "vectors_indexed", True)
    knowledge._set_embedding_success_status(content, documents)
    validate(content)
    knowledge._update_content(content)
