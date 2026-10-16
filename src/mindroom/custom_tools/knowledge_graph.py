"""Read-only lookups in the calling agent's knowledge graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.knowledge_graph.scope import graph_scope
from mindroom.knowledge_graph.store import knowledge_graph_store
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge_graph.store import GraphFact
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_MAX_LIMIT = 50


def _date(value: datetime | None) -> str:
    return value.date().isoformat() if value is not None else "unknown"


def _render_fact(fact: GraphFact) -> str:
    if fact.invalid_at is not None:
        validity = f"valid {_date(fact.valid_at)} until {_date(fact.invalid_at)} (superseded)"
    else:
        validity = f"valid since {_date(fact.valid_at)}" if fact.valid_at is not None else "valid since unknown"
    sources = "; ".join(f"{source.description} ({_date(source.observed_at)})" for source in fact.sources) or "unknown"
    if (earlier := fact.source_count - len(fact.sources)) > 0:
        sources = f"{sources}; and {earlier} earlier"
    return f"- {fact.fact}\n  {validity}\n  sources: {sources}"


class KnowledgeGraphTools(Toolkit):
    """Search the facts and entities this agent's knowledge graph holds, with when each held and its evidence."""

    def __init__(
        self,
        agent_name: str,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._runtime_paths = runtime_paths
        self._execution_identity = execution_identity
        super().__init__(name="knowledge_graph", tools=[self.search_graph, self.graph_entity])

    async def search_graph(self, query: str, limit: int = 10) -> str:
        """Find facts about people, projects, documents, and events that your conversations and tools revealed.

        Each fact shows when it held and its sources: "conversation" means someone said it, "tool_results" means a
        tool returned it. Facts marked superseded were true before later evidence replaced them.

        Args:
            query: What to look up, for example "who owns Project Atlas".
            limit: Most facts to return, from 1 to 50.

        Returns:
            Matching facts with their validity and sources.

        """
        return await self._lookup(query, limit=max(1, min(limit, _MAX_LIMIT)))

    async def graph_entity(self, name: str) -> str:
        """Show what the knowledge graph knows about one person, organization, project, or other entity.

        Args:
            name: The entity's name, for example "Alice" or "Project Atlas".

        Returns:
            The entity's summary and the facts that touch it.

        """
        return await self._lookup(name, limit=None)

    async def _lookup(self, text: str, *, limit: int | None) -> str:
        store = knowledge_graph_store()
        if store is None:
            return "The knowledge graph is not running."
        scope = graph_scope(self._agent_name, self._config, self._runtime_paths, self._execution_identity)
        try:
            if limit is not None:
                facts = await store.search_facts(scope, text, limit)
                return "\n".join(_render_fact(fact) for fact in facts) or f"No facts match {text!r}."
            entity = await store.entity(scope, text)
        except Exception as exc:
            # The error can carry request data or internals, so only the log sees it.
            logger.warning(
                "Knowledge graph lookup failed",
                agent=self._agent_name,
                error=f"{type(exc).__name__}: {exc}",
            )
            return "The knowledge graph is unavailable right now; try again later."
        if entity is None:
            return f"No entity matches {text!r}."
        facts = "\n".join(_render_fact(fact) for fact in entity.facts) or "No facts yet."
        return f"{entity.name}: {entity.summary}\n{facts}"
