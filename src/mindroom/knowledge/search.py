"""What an agent searches: its published knowledge sources, the search tool the model calls, and its prompt block."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agno.tools.function import Function

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.knowledge.read_protocol import SearchHit

logger = get_logger(__name__)

KNOWLEDGE_SEARCH_TOOL_NAME = "search_knowledge_base"
_DEFAULT_MAX_RESULTS = 10
_MAX_MERGED_SOURCE_COVERAGE_RESULTS = 20
# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
# The prompt text and the tool's docstring are Agno's, so prompts and tool schemas stay byte-identical.
_SEARCH_INSTRUCTIONS = (
    "You have a knowledge base you can search using the search_knowledge_base tool. "
    "Search before answering questions—don't assume you know the answer. "
    "For ambiguous questions, search first rather than asking for clarification."
)
_NO_DOCUMENTS = "No documents found"


class _KnowledgeIndex(Protocol):
    """One published index a source searches."""

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        """Return at most ``limit`` hits, nearest first; raise when the search fails."""
        ...

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        """Return at most ``limit`` hits without blocking the event loop."""
        ...


@dataclass(frozen=True)
class KnowledgeSourceDescription:
    """Agent-visible description of one queryable knowledge source."""

    base_id: str
    description: str


@dataclass(frozen=True)
class KnowledgeSource:
    """One knowledge base an agent searches: what the model is told about it, and its published index."""

    description: KnowledgeSourceDescription
    index: _KnowledgeIndex


def knowledge_search_tool_description(
    sources: Sequence[KnowledgeSourceDescription],
    *,
    memory_search_available: bool,
) -> str:
    """Build the description shown to the model for the knowledge-search tool."""
    if not sources:
        return "Search this agent's configured knowledge bases for information about a query."

    lines = [
        "Search this agent's configured knowledge bases for information about a query.",
        "Available sources:",
    ]
    for source in sources:
        description = source.description or "No description configured."
        lines.append(f"- {source.base_id}: {description}")
    lines.append("This list only describes sources available through search_knowledge_base.")
    if memory_search_available:
        lines.append("For resilient memory search, team-visible memory, and memory IDs, use search_memories.")
    return "\n".join(lines)


def _format_search_hits(hits: Sequence[SearchHit]) -> str:
    """Render hits as the JSON the search tool has always returned, or its no-result text."""
    if not hits:
        return _NO_DOCUMENTS
    documents = [
        {
            **({"name": hit.name} if hit.name is not None else {}),
            "meta_data": hit.meta_data,
            "content": hit.content,
        }
        for hit in hits
    ]
    return json.dumps(documents, indent=2, default=str, ensure_ascii=False)


def _interleave(results_by_source: list[list[SearchHit]], limit: int) -> list[SearchHit]:
    """Interleave per-source results so one knowledge base cannot dominate the top hits."""
    merged: list[SearchHit] = []
    index = 0
    while len(merged) < limit:
        added = False
        for results in results_by_source:
            if index < len(results):
                merged.append(results[index])
                added = True
                if len(merged) >= limit:
                    return merged
        if not added:
            break
        index += 1
    return merged


def _merged(
    outcomes: Sequence[tuple[KnowledgeSource, list[SearchHit] | Exception]],
    limit: int,
) -> list[SearchHit]:
    """Merge the sources that answered; when every source failed, raise the first failure.

    A partial failure warns and searches on, so one unavailable base does not
    hide the others' results, but an all-failed search must not look like an
    empty index (ISSUE-237).
    """
    answered: list[list[SearchHit]] = []
    first_error: Exception | None = None
    for source, outcome in outcomes:
        if isinstance(outcome, Exception):
            first_error = first_error or outcome
            logger.warning(
                "Knowledge source search failed",
                base_id=source.description.base_id,
                exc_info=outcome,
            )
            continue
        answered.append(outcome)
    if first_error is not None and not answered:
        raise first_error
    return _interleave(answered, limit)


@dataclass(frozen=True)
class AgentKnowledge:
    """The knowledge sources one agent searches through ``search_knowledge_base``."""

    sources: tuple[KnowledgeSource, ...]

    @property
    def max_results(self) -> int:
        """Return how many hits one search returns, enough for every source to contribute."""
        return max(min(len(self.sources), _MAX_MERGED_SOURCE_COVERAGE_RESULTS), _DEFAULT_MAX_RESULTS)

    def search(self, query: str, limit: int | None = None) -> list[SearchHit]:
        """Search every source in turn and interleave their hits."""
        effective_limit = limit or self.max_results
        outcomes: list[tuple[KnowledgeSource, list[SearchHit] | Exception]] = []
        for source in self.sources:
            try:
                outcomes.append((source, source.index.search(query, limit=effective_limit)))
            except Exception as exc:
                outcomes.append((source, exc))
        return _merged(outcomes, effective_limit)

    async def asearch(self, query: str, limit: int | None = None) -> list[SearchHit]:
        """Search sources sequentially, so one query cannot exhaust the native reader slots."""
        effective_limit = limit or self.max_results
        outcomes: list[tuple[KnowledgeSource, list[SearchHit] | Exception]] = []
        for source in self.sources:
            try:
                outcomes.append((source, await source.index.asearch(query, limit=effective_limit)))
            except Exception as exc:
                outcomes.append((source, exc))
        return _merged(outcomes, effective_limit)

    async def _search_tool_result(self, query: str) -> str:
        """Return what ``search_knowledge_base`` tells the model: the hits, no-result text, or the failure's type."""
        try:
            hits = await self.asearch(query)
        except Exception as exc:
            logger.warning("Knowledge search failed", error_type=type(exc).__name__)
            return f"Error searching knowledge base: {type(exc).__name__}"
        return _format_search_hits(hits)

    def search_function(self, *, memory_search_available: bool = False) -> Function:
        """Return the ``search_knowledge_base`` tool, authored as an Agno function so Agno generates its schema.

        ``adapt_function`` turns it into the ``ToolFunction`` an agent offers.
        """

        async def search_knowledge_base(query: str) -> str:
            """Use this function to search the knowledge base for information about a query.

            Args:
                query: The query to search for.

            Returns:
                str: A string containing the response from the knowledge base.

            """
            return await self._search_tool_result(query)

        function = Function.from_callable(search_knowledge_base, name=KNOWLEDGE_SEARCH_TOOL_NAME)
        function.description = knowledge_search_tool_description(
            tuple(source.description for source in self.sources),
            memory_search_available=memory_search_available,
        )
        return function

    def prompt_block(self) -> str:
        """Return the system-prompt block that tells the model to search before answering."""
        return f"<knowledge_base>\n{_SEARCH_INSTRUCTIONS}\n</knowledge_base>"
