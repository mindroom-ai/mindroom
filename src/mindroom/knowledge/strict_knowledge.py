"""The Agno ``Knowledge`` handle an Agno agent receives, whose search and prompt block are MindRoom's own.

An Agno agent builds its ``search_knowledge_base`` tool and ``<knowledge_base>``
prompt block from a ``Knowledge`` object, so until the owned agent loop runs
agents this handle carries an ``AgentKnowledge`` and answers both from it.
Searches propagate failures instead of returning ``[]`` (Agno's tool reports
the exception type), and the ``user_id`` Agno forwards is dropped because
MindRoom knowledge is shared, not per user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.knowledge.document import Document
from agno.knowledge.knowledge import Knowledge

if TYPE_CHECKING:
    from mindroom.knowledge.read_protocol import SearchHit
    from mindroom.knowledge.search import AgentKnowledge


def _documents(hits: list[SearchHit]) -> list[Document]:
    return [Document(content=hit.content, name=hit.name, meta_data=dict(hit.meta_data)) for hit in hits]


class StrictSearchKnowledge(Knowledge):
    """Agno knowledge handle delegating search and its prompt block to one agent's ``AgentKnowledge``."""

    def __init__(self, agent_knowledge: AgentKnowledge) -> None:
        super().__init__(max_results=agent_knowledge.max_results)
        self.agent_knowledge = agent_knowledge

    def search(
        self,
        query: str,
        max_results: int | None = None,
        filters: dict[str, Any] | list[Any] | None = None,
        search_type: str | None = None,
        user_id: str | None = None,
    ) -> list[Document]:
        """Return matching documents; raise when every source fails."""
        del filters, search_type, user_id  # MindRoom knowledge is shared and never filtered.
        return _documents(self.agent_knowledge.search(query, max_results))

    async def asearch(
        self,
        query: str,
        max_results: int | None = None,
        filters: dict[str, Any] | list[Any] | None = None,
        search_type: str | None = None,
        user_id: str | None = None,
    ) -> list[Document]:
        """Async variant of ``search``."""
        del filters, search_type, user_id
        return _documents(await self.agent_knowledge.asearch(query, max_results))

    def build_context(
        self,
        enable_agentic_filters: bool = False,
        user_id: str | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> str:
        """Return the agent's ``<knowledge_base>`` prompt block; MindRoom never enables agentic filters."""
        del enable_agentic_filters, user_id, kwargs
        return self.agent_knowledge.prompt_block()
