"""The runtime's one Graphiti client and the graph operations MindRoom uses.

Graphiti, FalkorDB, and the embedded server load on first use, so a runtime without graph-enabled agents never
imports them. Each scope's graph is addressed by its ``graph_name``, which FalkorDB keeps as a separate graph.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlsplit

from mindroom.embeddings.factory import create_configured_embedder, embedder_client_signature
from mindroom.knowledge_graph.usage import usage_target
from mindroom.logging_config import get_logger
from mindroom.tool_system.dependencies import ensure_tool_deps, optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from graphiti_core import Graphiti
    from graphiti_core.driver.driver import GraphDriver
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EpisodicNode

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge_graph.embedded_server import EmbeddedServer
    from mindroom.knowledge_graph.scope import GraphScope
    from mindroom.knowledge_graph.usage import UsageTarget

logger = get_logger(__name__)

type _EpisodeKind = Literal["conversation", "tool_results"]

_DOTENV_DISABLED = "PYTHON_DOTENV_DISABLED"
# Facts listed for one entity, current ones first.
_MAX_ENTITY_FACTS = 30


@dataclass(frozen=True)
class EpisodeInput:
    """One episode for Graphiti to extract entities and facts from."""

    kind: _EpisodeKind
    name: str
    body: str
    source_description: str
    reference_time: datetime


@dataclass(frozen=True)
class _FactSource:
    """An episode that supports a fact: its description, which starts with its kind, and when it was observed."""

    description: str
    observed_at: datetime | None


@dataclass(frozen=True)
class GraphFact:
    """A fact with the window it held in; ``invalid_at`` is set once later evidence superseded it."""

    fact: str
    valid_at: datetime | None
    invalid_at: datetime | None
    sources: list[_FactSource]


@dataclass(frozen=True)
class _GraphEntity:
    """An entity's summary and the facts that touch it."""

    name: str
    summary: str
    facts: list[GraphFact]


class GraphStore(Protocol):
    """The graph operations ingestion and the tool use."""

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        """Extract an episode into the scope's graph, charging extraction to ``usage``."""

    async def search_facts(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]:
        """Return the scope's facts most relevant to ``query``."""

    async def entity(self, scope: GraphScope, name: str) -> _GraphEntity | None:
        """Return the scope's entity best matching ``name``, or None."""

    async def close(self) -> None:
        """Release the database connection."""


_failure: str | None = None
_runtime_store: GraphStore | None = None


def knowledge_graph_store() -> GraphStore | None:
    """Return the running runtime's graph store, or None while no agent enables the graph."""
    return _runtime_store


def set_knowledge_graph_store(store: GraphStore | None) -> None:
    """Register the runtime's one graph store, or clear it with None."""
    global _runtime_store
    _runtime_store = store


def knowledge_graph_failure() -> str | None:
    """Return why the knowledge graph last failed, or None once it works again."""
    return _failure


def record_knowledge_graph_failure(detail: str | None) -> None:
    """Record a failure for ``/api/health``, or clear it with None."""
    global _failure
    _failure = detail


def _import_graphiti() -> None:
    """Import Graphiti and MindRoom's clients for it once, inside the optional-dependency scope.

    Graphiti calls ``load_dotenv()`` at import, which copies the first ``.env`` above its install into ``os.environ``;
    MindRoom reads ``.env`` files only deliberately, so that load is switched off while Graphiti imports.
    """
    previous = os.environ.get(_DOTENV_DISABLED)
    os.environ[_DOTENV_DISABLED] = "1"
    try:
        with optional_dependency_environment():
            import graphiti_core.driver.falkordb_driver  # noqa: PLC0415
            import graphiti_core.graphiti  # noqa: F401, PLC0415

            import mindroom.knowledge_graph.clients  # noqa: F401, PLC0415
    finally:
        if previous is None:
            os.environ.pop(_DOTENV_DISABLED, None)
        else:
            os.environ[_DOTENV_DISABLED] = previous


async def _facts(driver: GraphDriver, edges: Sequence[EntityEdge]) -> list[GraphFact]:
    """Return edges as facts with the episodes that support them."""
    from graphiti_core.nodes import EpisodicNode  # noqa: PLC0415

    episode_uuids = sorted({uuid for edge in edges for uuid in edge.episodes})
    episodes: dict[str, EpisodicNode] = {}
    if episode_uuids:
        episodes = {episode.uuid: episode for episode in await EpisodicNode.get_by_uuids(driver, episode_uuids)}
    return [
        GraphFact(
            fact=edge.fact,
            valid_at=edge.valid_at,
            invalid_at=edge.invalid_at,
            sources=[
                _FactSource(description=episode.source_description, observed_at=episode.valid_at)
                for uuid in edge.episodes
                if (episode := episodes.get(uuid)) is not None
            ],
        )
        for edge in edges
    ]


class GraphitiStore:
    """Own one Graphiti client, rebuilt when the database or embedder settings change."""

    def __init__(self, config_provider: Callable[[], Config], runtime_paths: RuntimePaths) -> None:
        self._config_provider = config_provider
        self._runtime_paths = runtime_paths
        self._lock = asyncio.Lock()
        self._graphiti: Graphiti | None = None
        self._signature: tuple[str, str | None, str] | None = None
        self._embedded_server: EmbeddedServer | None = None

    async def _graphiti_client(self) -> Graphiti:
        config = self._config_provider()
        settings = config.knowledge_graph
        signature = (settings.backend, settings.url, embedder_client_signature(config, self._runtime_paths))
        async with self._lock:
            if self._graphiti is not None and self._signature == signature:
                return self._graphiti
            await self._close_unlocked()
            self._graphiti = await self._build(config)
            self._signature = signature
            return self._graphiti

    async def _build(self, config: Config) -> Graphiti:
        await asyncio.to_thread(ensure_tool_deps, ["graphiti-core"], "knowledge_graph", self._runtime_paths)
        await asyncio.to_thread(_import_graphiti)
        # why-lazy: Graphiti, FalkorDB, and the embedded server are optional and load only for graph-enabled runtimes.
        from graphiti_core import Graphiti  # noqa: PLC0415
        from graphiti_core.driver.falkordb_driver import FalkorDriver  # noqa: PLC0415

        from mindroom.knowledge_graph.clients import (  # noqa: PLC0415
            MindRoomEmbedder,
            MindRoomLLMClient,
            UnusedCrossEncoder,
        )

        settings = config.knowledge_graph
        if settings.backend == "falkordb_lite":
            from falkordb.asyncio import FalkorDB  # noqa: PLC0415

            from mindroom.knowledge_graph.embedded_server import start_embedded_server  # noqa: PLC0415

            server = await start_embedded_server(self._runtime_paths.storage_root / "knowledge_graph")
            self._embedded_server = server
            driver = FalkorDriver(falkor_db=FalkorDB(unix_socket_path=server.socket_path))
        else:
            url = urlsplit(settings.url or "")
            driver = FalkorDriver(
                host=url.hostname or "localhost",
                port=url.port or 6379,
                username=url.username,
                password=url.password,
            )
        return Graphiti(
            graph_driver=driver,
            llm_client=MindRoomLLMClient(self._config_provider, self._runtime_paths),
            embedder=MindRoomEmbedder(create_configured_embedder(config, self._runtime_paths)),
            cross_encoder=UnusedCrossEncoder(),
        )

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        """Extract an episode into the scope's graph, charging extraction to ``usage``."""
        from graphiti_core.nodes import EpisodeType  # noqa: PLC0415

        graphiti = await self._graphiti_client()
        with usage_target(usage):
            await graphiti.add_episode(
                name=episode.name,
                episode_body=episode.body,
                source_description=episode.source_description,
                reference_time=episode.reference_time,
                source=EpisodeType.message if episode.kind == "conversation" else EpisodeType.text,
                group_id=scope.graph_name,
            )

    async def search_facts(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]:
        """Return the scope's facts most relevant to ``query``, without an LLM call."""
        from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF  # noqa: PLC0415

        graphiti = await self._graphiti_client()
        results = await graphiti.search_(
            query,
            config=EDGE_HYBRID_SEARCH_RRF.model_copy(update={"limit": limit}),
            group_ids=[scope.graph_name],
        )
        return await _facts(graphiti.driver.clone(database=scope.graph_name), results.edges)

    async def entity(self, scope: GraphScope, name: str) -> _GraphEntity | None:
        """Return the scope's entity best matching ``name`` with the facts that touch it."""
        from graphiti_core.edges import EntityEdge  # noqa: PLC0415
        from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF  # noqa: PLC0415

        graphiti = await self._graphiti_client()
        results = await graphiti.search_(
            name,
            config=NODE_HYBRID_SEARCH_RRF.model_copy(update={"limit": 1}),
            group_ids=[scope.graph_name],
        )
        if not results.nodes:
            return None
        node = results.nodes[0]
        driver = graphiti.driver.clone(database=scope.graph_name)
        edges = await EntityEdge.get_by_node_uuid(driver, node.uuid)
        # Current facts first, then the most recently valid.
        edges.sort(key=lambda edge: (edge.invalid_at is not None, -(edge.valid_at or edge.created_at).timestamp()))
        return _GraphEntity(
            name=node.name,
            summary=node.summary,
            facts=await _facts(driver, edges[:_MAX_ENTITY_FACTS]),
        )

    async def close(self) -> None:
        """Close the client and stop the embedded database server."""
        async with self._lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        graphiti, server = self._graphiti, self._embedded_server
        self._graphiti, self._embedded_server, self._signature = None, None, None
        if graphiti is not None:
            await graphiti.close()
        if server is not None:
            await server.stop()
