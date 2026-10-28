"""Lookups in the calling agent's knowledge graph, and the edits that clean it up where the agent may make them."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.config.knowledge_graph import KNOWLEDGE_GRAPH_SOURCE_ID
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.knowledge_graph.scope import graph_scope
from mindroom.knowledge_graph.store import knowledge_graph_store
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge_graph.scope import GraphScope
    from mindroom.knowledge_graph.store import GraphFact, GraphStore
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_MAX_LIMIT = 50
_KNOWLEDGE_SOURCE_DESCRIPTION = (
    "Facts about people, projects, documents, and events that your conversations and tools revealed, "
    "with when each held and its sources."
)


def _date(value: datetime | None) -> str:
    return value.date().isoformat() if value is not None else "unknown"


def _render_fact(fact: GraphFact, *, with_id: bool = False) -> str:
    if fact.invalid_at is not None:
        validity = f"valid {_date(fact.valid_at)} until {_date(fact.invalid_at)} (superseded)"
    else:
        validity = f"valid since {_date(fact.valid_at)}"
    sources = "; ".join(f"{source.description} ({_date(source.observed_at)})" for source in fact.sources) or "unknown"
    if (earlier := fact.source_count - len(fact.sources)) > 0:
        sources = f"{sources}; and {earlier} earlier"
    fact_id = f"\n  id: {fact.uuid}" if with_id else ""
    return f"- {fact.fact}{fact_id}\n  {validity}\n  sources: {sources}"


@dataclass(frozen=True)
class _GraphFactIndex:
    """The caller's own graph as an index ``search_knowledge_base`` searches, whose hits are facts."""

    agent_name: str
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        store = knowledge_graph_store()
        if store is None:
            msg = "The knowledge graph is not running"
            raise RuntimeError(msg)
        scope = graph_scope(self.agent_name, self.config, self.runtime_paths, self.execution_identity)
        facts = await store.search_facts(scope, query, limit)
        return [
            SearchHit(content=_render_fact(fact), meta_data={"source": KNOWLEDGE_GRAPH_SOURCE_ID}) for fact in facts
        ]


def with_knowledge_graph(
    knowledge: AgentKnowledge | None,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> AgentKnowledge:
    """Return ``knowledge`` with the caller's graph as one more source, so a knowledge search also finds its facts."""
    graph = KnowledgeSource(
        description=KnowledgeSourceDescription(
            base_id=KNOWLEDGE_GRAPH_SOURCE_ID,
            description=_KNOWLEDGE_SOURCE_DESCRIPTION,
        ),
        index=_GraphFactIndex(agent_name, config, runtime_paths, execution_identity),
    )
    if knowledge is None:
        return AgentKnowledge(sources=(graph,))
    return replace(knowledge, sources=(*knowledge.sources, graph))


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
        self._edits = config.resolve_agent(agent_name).knowledge_graph_edits
        edits = (
            [
                self.delete_graph_fact,
                self.delete_graph_entity,
                self.merge_graph_entities,
                self.update_graph_entity_summary,
            ]
            if self._edits
            else []
        )
        super().__init__(name="knowledge_graph", tools=[self.search_graph, self.graph_entity, *edits])

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

        async def search(store: GraphStore, scope: GraphScope) -> str:
            facts = await store.search_facts(scope, query, max(1, min(limit, _MAX_LIMIT)))
            return "\n".join(_render_fact(fact, with_id=self._edits) for fact in facts) or f"No facts match {query!r}."

        return await self._in_graph("search_graph", search)

    async def graph_entity(self, name: str) -> str:
        """Show what the knowledge graph knows about one person, organization, project, or other entity.

        An entity named exactly ``name`` in any letter case is returned, and a configured alias, agent key, or Matrix
        ID finds the entity it names; otherwise the closest match, followed by the names of other close matches to look
        up if it is not the one you meant.

        Args:
            name: The entity's name, for example "Alice" or "Project Atlas".

        Returns:
            The entity's summary and the facts that touch it.

        """

        async def look_up(store: GraphStore, scope: GraphScope) -> str:
            entity = await store.entity(scope, name)
            if entity is None:
                return f"No entity matches {name!r}."
            facts = "\n".join(_render_fact(fact, with_id=self._edits) for fact in entity.facts) or "No facts yet."
            label = "Other entities under this name" if entity.exact else "Other close matches"
            if self._edits:
                rendered = f"{entity.name} (id {entity.uuid}): {entity.summary}\n{facts}"
                others = [f"{other.name} (id {other.uuid})" for other in entity.others]
            else:
                rendered = f"{entity.name}: {entity.summary}\n{facts}"
                # Without ids, the other entities found under the name asked for cannot be looked up.
                others = [] if entity.exact else [other.name for other in entity.others]
            if others:
                rendered += f"\n{label}: {', '.join(others)}"
            return rendered

        return await self._in_graph("graph_entity", look_up)

    async def delete_graph_fact(self, fact_id: str) -> str:
        """Delete a wrong fact from your knowledge graph.

        Delete a fact that never held, names the wrong person or thing, or records your own lookups instead of a fact
        about your subjects. A fact that held and later changed is history: keep it, because newer evidence marks it
        superseded. A later conversation or tool result that states the fact again can add it back. The summaries of
        the entities it connects can still state it; rewrite those with `update_graph_entity_summary`.

        Args:
            fact_id: The fact's id, as `search_graph` and `graph_entity` show it.

        Returns:
            The deleted fact, or that no fact has this id.

        """

        async def delete(store: GraphStore, scope: GraphScope) -> str:
            fact = await store.delete_fact(scope, fact_id)
            if fact is None:
                return f"No fact in your graph has id {fact_id!r}."
            return f"Deleted the fact {fact!r}."

        return await self._in_graph("delete_graph_fact", delete)

    async def delete_graph_entity(self, entity_id: str) -> str:
        """Delete an entity that does not belong in your knowledge graph, with every fact that touches it.

        Delete an entity that is not a person, agent, organization, location, event, project, product, document, or
        account, such as a file path, URL, or setting, or that never existed. To combine two entities for the same
        person or thing, merge them with `merge_graph_entities` instead, which keeps the facts.

        Args:
            entity_id: The entity's id, as `graph_entity` shows it.

        Returns:
            The deleted entity's name, or that no entity has this id.

        """

        async def delete(store: GraphStore, scope: GraphScope) -> str:
            name = await store.delete_entity(scope, entity_id)
            if name is None:
                return f"No entity in your graph has id {entity_id!r}."
            return f"Deleted {name!r} and every fact that touched it."

        return await self._in_graph("delete_graph_entity", delete)

    async def merge_graph_entities(self, duplicate_id: str, into_id: str) -> str:
        """Merge an entity into another entity for the same person or thing, such as a nickname into a full name.

        The duplicate's facts move onto the other entity, which keeps its name and summary; facts between the two are
        dropped, and the duplicate is deleted. `graph_entity` also takes an id, to check an entity before merging it.

        Args:
            duplicate_id: The id of the entity to merge away, as `graph_entity` shows it.
            into_id: The id of the entity to keep.

        Returns:
            Both entities' names and how many facts moved, or that an id is unknown.

        """
        if duplicate_id == into_id:
            return "An entity cannot be merged into itself."

        async def merge(store: GraphStore, scope: GraphScope) -> str:
            merged = await store.merge_entities(scope, duplicate_id, into_id)
            if merged is None:
                return f"{duplicate_id!r} or {into_id!r} is not the id of an entity in your graph."
            facts = "fact" if merged.moved_facts == 1 else "facts"
            return f"Merged {merged.duplicate!r} into {merged.into!r}, moving {merged.moved_facts} {facts}."

        return await self._in_graph("merge_graph_entities", merge)

    async def update_graph_entity_summary(self, entity_id: str, summary: str) -> str:
        """Replace an entity's summary, for example to remove what a fact you deleted said.

        Keep everything the old summary says that is still right; later conversations and tool results that mention
        the entity add to the new summary.

        Args:
            entity_id: The entity's id, as `graph_entity` shows it.
            summary: The new summary.

        Returns:
            The entity's name, or that no entity has this id.

        """

        async def update(store: GraphStore, scope: GraphScope) -> str:
            name = await store.update_entity_summary(scope, entity_id, summary)
            if name is None:
                return f"No entity in your graph has id {entity_id!r}."
            return f"Updated the summary of {name!r}."

        return await self._in_graph("update_graph_entity_summary", update)

    async def _in_graph(self, function: str, work: Callable[[GraphStore, GraphScope], Awaitable[str]]) -> str:
        """Run ``work`` on the caller's own graph, turning a failure into a message the agent can relay."""
        store = knowledge_graph_store()
        if store is None:
            return "The knowledge graph is not running."
        scope = graph_scope(self._agent_name, self._config, self._runtime_paths, self._execution_identity)
        try:
            return await work(store, scope)
        except Exception as exc:
            # The error can carry request data or internals, so only the log sees it.
            logger.warning(
                "Knowledge graph call failed",
                agent=self._agent_name,
                function=function,
                error=f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            return "The knowledge graph is unavailable right now; try again later."
