"""Lookups in the calling agent's knowledge graph, and the edits that clean it up where the agent may make them."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from mindroom.config.knowledge_graph import (
    KNOWLEDGE_GRAPH_EDIT_ACTIONS,
    KNOWLEDGE_GRAPH_READ_ACTIONS,
    KNOWLEDGE_GRAPH_SOURCE_ID,
)
from mindroom.config.resolution import edits_knowledge_graph
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.knowledge_graph.scope import graph_scope
from mindroom.knowledge_graph.store import knowledge_graph_store
from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.native_helpers import reject_unexpected_args, restrict_actions

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import datetime

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge_graph.scope import GraphScope
    from mindroom.knowledge_graph.store import GraphFact, GraphStore
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_MAX_LIMIT = 50
_KNOWLEDGE_SOURCE_DESCRIPTION = (
    "Facts about people, projects, documents, and events that your conversations and tools revealed, "
    "with when each held and its sources."
)
_GraphAction = Literal["search", "entity", "update_summary", "merge", "delete_fact", "delete_entity"]
_ACTION_FIELDS: dict[str, str] = {
    "search": "query limit",
    "entity": "query",
    "update_summary": "entity_id summary",
    "merge": "duplicate_id into_id",
    "delete_fact": "fact_id",
    "delete_entity": "entity_id",
}
_WRITE_REQUIRED: dict[str, tuple[str, ...]] = {
    "update_summary": ("entity_id", "summary"),
    "merge": ("duplicate_id", "into_id"),
    "delete_fact": ("fact_id",),
    "delete_entity": ("entity_id",),
}


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
        self._edits = edits_knowledge_graph(config, agent_name)
        super().__init__(name="knowledge_graph", tools=[self.knowledge_graph])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Restrict ``knowledge_graph`` to read actions and fields when edits are disabled."""
        if function.name == "knowledge_graph" and not self._edits:
            return restrict_actions(function, KNOWLEDGE_GRAPH_READ_ACTIONS, action_fields=_ACTION_FIELDS)
        return function

    async def _search(self, query: str, limit: int) -> str:
        async def search(store: GraphStore, scope: GraphScope) -> str:
            facts = await store.search_facts(scope, query, max(1, min(limit, _MAX_LIMIT)))
            return "\n".join(_render_fact(fact, with_id=self._edits) for fact in facts) or f"No facts match {query!r}."

        return await self._in_graph("knowledge_graph:search", search)

    async def _entity(self, name: str) -> str:
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

        return await self._in_graph("knowledge_graph:entity", look_up)

    async def knowledge_graph(  # noqa: PLR0911
        self,
        action: _GraphAction,
        query: str | None = None,
        limit: int | None = None,
        fact_id: str | None = None,
        entity_id: str | None = None,
        duplicate_id: str | None = None,
        into_id: str | None = None,
        summary: str | None = None,
    ) -> str:
        """Access facts and entities in your knowledge graph.

        - `search`: Find facts matching `query` (optional `limit`, 1..50, default 10) with validity and sources ("conversation" = said, "tool_results" = tool output, superseded = replaced by later evidence).
        - `entity`: Show summary and facts for `query` (exact case-insensitive name, alias, agent key, Matrix ID, or entity ID when edits are enabled; otherwise closest match plus other close matches).
        - `update_summary`: Replace `entity_id`'s summary with `summary`, keeping everything still right.
        - `merge`: Merge `duplicate_id` into `into_id`, moving its facts onto `into_id`, dropping facts between them, and deleting the duplicate.
        - `delete_fact`: Delete a wrong `fact_id` that never held or recorded your own lookup; keep historical superseded facts.
        - `delete_entity`: Delete a non-entity `entity_id` (e.g. path, URL, setting) and every fact touching it; use `merge` for duplicates.
        """
        if action in KNOWLEDGE_GRAPH_EDIT_ACTIONS and not self._edits:
            return "Error: knowledge_graph_edits is not enabled for this agent."
        if action not in _ACTION_FIELDS:
            allowed = KNOWLEDGE_GRAPH_READ_ACTIONS if not self._edits else tuple(_ACTION_FIELDS)
            return f"Error: Unsupported action {action!r}. Expected one of: {', '.join(allowed)}."
        params: dict[str, object] = {
            "query": query,
            "limit": limit,
            "fact_id": fact_id,
            "entity_id": entity_id,
            "duplicate_id": duplicate_id,
            "into_id": into_id,
            "summary": summary,
        }
        if (
            error := reject_unexpected_args(
                action,
                _ACTION_FIELDS,
                {name: value is not None for name, value in params.items()},
            )
        ) is not None:
            return f"Error: {error}"
        if action == "search":
            if not isinstance(query, str) or not query.strip():
                return "Error: Parameter(s) query required for action 'search'."
            return await self._search(query, 10 if limit is None else limit)
        if action == "entity":
            if not isinstance(query, str) or not query.strip():
                return "Error: Parameter(s) query required for action 'entity'."
            return await self._entity(query)
        required = _WRITE_REQUIRED[action]
        if missing := [field for field in required if params[field] is None]:
            return f"Error: Parameter(s) {', '.join(missing)} required for action {action!r}."
        if action == "merge" and duplicate_id == into_id:
            return "An entity cannot be merged into itself."
        return await self._in_graph(
            f"knowledge_graph:{action}",
            lambda store, scope: self._apply_write(
                store,
                scope,
                action,
                fact_id=fact_id,
                entity_id=entity_id,
                duplicate_id=duplicate_id,
                into_id=into_id,
                summary=summary,
            ),
        )

    @staticmethod
    async def _apply_write(
        store: GraphStore,
        scope: GraphScope,
        action: Literal["update_summary", "merge", "delete_fact", "delete_entity"],
        *,
        fact_id: str | None,
        entity_id: str | None,
        duplicate_id: str | None,
        into_id: str | None,
        summary: str | None,
    ) -> str:
        if action == "update_summary" and entity_id is not None and summary is not None:
            name = await store.update_entity_summary(scope, entity_id, summary)
            return (
                f"Updated the summary of {name!r}."
                if name is not None
                else f"No entity in your graph has id {entity_id!r}."
            )
        if action == "merge" and duplicate_id is not None and into_id is not None:
            merged = await store.merge_entities(scope, duplicate_id, into_id)
            if merged is None:
                return f"{duplicate_id!r} or {into_id!r} is not the id of an entity in your graph."
            facts = "fact" if merged.moved_facts == 1 else "facts"
            return f"Merged {merged.duplicate!r} into {merged.into!r}, moving {merged.moved_facts} {facts}."
        if action == "delete_fact" and fact_id is not None:
            fact = await store.delete_fact(scope, fact_id)
            return f"Deleted the fact {fact!r}." if fact is not None else f"No fact in your graph has id {fact_id!r}."
        assert entity_id is not None
        name = await store.delete_entity(scope, entity_id)
        return (
            f"Deleted {name!r} and every fact that touched it."
            if name is not None
            else f"No entity in your graph has id {entity_id!r}."
        )

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
