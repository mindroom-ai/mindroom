"""The runtime's one Graphiti client and the graph operations MindRoom uses.

Graphiti, FalkorDB, and the embedded server load on first use, so a runtime without graph-enabled agents never
imports them. Each scope's graph is addressed by its ``graph_name``, which FalkorDB keeps as a separate graph.
"""

from __future__ import annotations

import asyncio
import functools
import os
import threading
from contextvars import copy_context
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from mindroom.embeddings.factory import create_configured_embedder, embedder_client_signature
from mindroom.knowledge_graph.extraction import ENTITY_TYPES, EXCLUDED_ENTITY_TYPES, EXTRACTION_INSTRUCTIONS
from mindroom.knowledge_graph.usage import usage_target
from mindroom.tool_system.dependencies import ensure_tool_deps, optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence
    from concurrent.futures import Future as ConcurrentFuture
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

type EpisodeKind = Literal["conversation", "tool_results"]

_DOTENV_DISABLED = "PYTHON_DOTENV_DISABLED"
# RediSearch rejects a fulltext query holding a word that is only a control character or an underscore, both of which
# Graphiti's FalkorDB sanitizer keeps (getzep/graphiti#1820); a tool's colored output carries such characters.
_CONTROL_CHARACTERS = str.maketrans(dict.fromkeys([*range(0x20), *range(0x7F, 0xA0)], " "))
# Facts listed for one entity, current ones first.
_MAX_ENTITY_FACTS = 30
# Entities a fuzzy lookup considers: the best hit is the answer and the rest are named as other close matches.
_ENTITY_CANDIDATES = 5
# FalkorDB's Cypher has no case-insensitive regex, so names are compared lowercased. Among several exact matches the
# one with the most facts wins, because a duplicate extracted once has few; the oldest, then the lowest uuid, settles a
# tie so the same name always finds the same entity.
_EXACT_ENTITY_QUERY = """
MATCH (n:Entity) WHERE toLower(n.name) = toLower($name)
OPTIONAL MATCH (n)-[r:RELATES_TO]-()
RETURN n.uuid AS uuid, n.created_at AS created_at, count(r) AS facts
ORDER BY facts DESC, created_at, uuid LIMIT 1
"""


@dataclass(frozen=True)
class EpisodeInput:
    """One episode for Graphiti to extract entities and facts from."""

    kind: EpisodeKind
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
    # The newest supporting episodes, oldest first, and how many support the fact in all.
    sources: list[_FactSource]
    source_count: int


@dataclass(frozen=True)
class _GraphEntity:
    """An entity's summary and the facts that touch it, and the names of the next closest entities after a fuzzy match."""

    name: str
    summary: str
    facts: list[GraphFact]
    others: list[str]


class GraphStore(Protocol):
    """The graph operations ingestion and the tool use."""

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        """Extract an episode into the scope's graph, charging extraction to ``usage``."""

    async def search_facts(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]:
        """Return the scope's facts most relevant to ``query``."""

    async def entity(self, scope: GraphScope, name: str) -> _GraphEntity | None:
        """Return the scope's entity named ``name`` in any letter case, else the best fuzzy match, or None."""


class UnusableExtractionError(ValueError):
    """The extraction model answered with output Graphiti cannot use; repeated ones drop the episode."""


_SHOWN_SOURCES = 3
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
            # Importing any part of Graphiti runs its package import, which loads `graphiti_core.graphiti` too.
            import graphiti_core.driver.falkordb_driver  # noqa: F401, PLC0415
            from graphiti_core.driver.falkordb import fulltext  # noqa: PLC0415

            import mindroom.knowledge_graph.clients  # noqa: F401, PLC0415

            if not hasattr(fulltext.sanitize_falkor_fulltext_query, "__wrapped__"):
                sanitize = _keep_parseable_words(fulltext.sanitize_falkor_fulltext_query)
                fulltext.sanitize_falkor_fulltext_query = sanitize  # ty: ignore[invalid-assignment]
    finally:
        if previous is None:
            os.environ.pop(_DOTENV_DISABLED, None)
        else:
            os.environ[_DOTENV_DISABLED] = previous


def _keep_parseable_words(sanitize: Callable[[str], str]) -> Callable[[str], str]:
    """Wrap Graphiti's fulltext sanitizer so it also drops control characters and words without a letter or digit."""

    @functools.wraps(sanitize)
    def sanitize_query(query: str) -> str:
        words = sanitize(query.translate(_CONTROL_CHARACTERS)).split()
        return " ".join(word for word in words if any(character.isalnum() for character in word))

    return sanitize_query


async def _facts(driver: GraphDriver, edges: Sequence[EntityEdge]) -> list[GraphFact]:
    """Return edges as facts with the newest episodes that support them.

    Graphiti appends an episode to a fact each time it sees the fact again, so a fact explored every day gathers
    hundreds; loading and listing them all would make every lookup grow without bound.
    """
    from graphiti_core.nodes import EpisodicNode  # noqa: PLC0415

    shown = {edge.uuid: edge.episodes[-_SHOWN_SOURCES:] for edge in edges}
    episode_uuids = sorted({uuid for uuids in shown.values() for uuid in uuids})
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
                for uuid in shown[edge.uuid]
                if (episode := episodes.get(uuid)) is not None
            ],
            source_count=len(edge.episodes),
        )
        for edge in edges
    ]


async def run_on_loop[T](loop: asyncio.AbstractEventLoop, coroutine: Coroutine[Any, Any, T]) -> T:
    """Await ``coroutine`` on ``loop`` with the caller's context variables; cancelling the caller cancels it."""
    if loop is asyncio.get_running_loop():
        return await coroutine
    future = cast("ConcurrentFuture[T]", copy_context().run(asyncio.run_coroutine_threadsafe, coroutine, loop))
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        # The work was cancelled on its own loop, for example by closing the store, not by this caller; a caller
        # such as an agent's tool call must see an ordinary failure rather than its own turn being cancelled.
        msg = "The work was cancelled on the other event loop"
        raise RuntimeError(msg) from None


class _GraphLoop:
    """The thread and event loop every Graphiti and FalkorDB call runs on.

    Graphiti spends hundreds of milliseconds of Python per episode on parsing and deduplication, which stalled every
    agent's replies while it shared the runtime loop. On its own thread it only competes for the GIL, which the
    interpreter hands over every few milliseconds.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name="mindroom-knowledge-graph", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def stop(self) -> None:
        """Cancel the work still on the loop, then stop it and wait for its thread."""
        await run_on_loop(self.loop, _cancel_other_tasks())
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.to_thread(self._thread.join)
        self.loop.close()


async def _cancel_other_tasks() -> None:
    current = asyncio.current_task()
    tasks = [task for task in asyncio.all_tasks() if task is not current]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class GraphitiStore:
    """Own one Graphiti client over the embedded database, rebuilt when the embedder settings change.

    The client, its database connection, and the embedded server live on the store's graph loop; the public methods
    run their work there and return the result to the caller's loop.
    """

    def __init__(
        self,
        config_provider: Callable[[], Config],
        runtime_paths: RuntimePaths,
        runtime_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._config_provider = config_provider
        self._runtime_paths = runtime_paths
        # The runtime's loop, which extraction calls go back to; taken from the owner, not from whoever calls first.
        self._runtime_loop = runtime_loop
        self._graph_loop: _GraphLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._graphiti: Graphiti | None = None
        self._signature: str | None = None
        self._embedded_server: EmbeddedServer | None = None

    async def _run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run ``coroutine`` on the graph loop, starting it on first use."""
        if self._graph_loop is None:
            self._graph_loop = _GraphLoop()
            self._lock = asyncio.Lock()
        return await run_on_loop(self._graph_loop.loop, coroutine)

    async def _graphiti_client(self) -> Graphiti:
        config = self._config_provider()
        signature = embedder_client_signature(config, self._runtime_paths)
        assert self._lock is not None, "the client is only used on the graph loop"
        async with self._lock:
            # A server that died, for example from running out of memory, is replaced rather than failing every call.
            server_alive = self._embedded_server is not None and self._embedded_server.process.returncode is None
            if self._graphiti is not None and self._signature == signature and server_alive:
                return self._graphiti
            await self._close_unlocked()
            self._graphiti = await self._build(config)
            self._signature = signature
            return self._graphiti

    async def _build(self, config: Config) -> Graphiti:
        await asyncio.to_thread(ensure_tool_deps, ["graphiti-core"], "knowledge_graph", self._runtime_paths)
        await asyncio.to_thread(_import_graphiti)
        # why-lazy: Graphiti, FalkorDB, and the embedded server are optional and load only for graph-enabled runtimes.
        from falkordb.asyncio import FalkorDB  # noqa: PLC0415
        from graphiti_core import Graphiti  # noqa: PLC0415

        from mindroom.knowledge_graph.clients import (  # noqa: PLC0415
            MindRoomEmbedder,
            MindRoomFalkorDriver,
            MindRoomLLMClient,
            UnusedCrossEncoder,
        )
        from mindroom.knowledge_graph.embedded_server import start_embedded_server  # noqa: PLC0415

        # A local embedder installs packages and loads its model when built.
        embedder = await asyncio.to_thread(create_configured_embedder, config, self._runtime_paths)
        server = await start_embedded_server(self._runtime_paths.storage_root / "knowledge_graph")
        self._embedded_server = server
        driver = MindRoomFalkorDriver(falkor_db=FalkorDB(unix_socket_path=server.socket_path))
        return Graphiti(
            graph_driver=driver,
            llm_client=MindRoomLLMClient(self._config_provider, self._runtime_paths, self._runtime_loop),
            embedder=MindRoomEmbedder(embedder),
            cross_encoder=UnusedCrossEncoder(),
        )

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        """Extract an episode into the scope's graph, charging extraction to ``usage``."""
        await self._run(self._add_episode(scope, episode, usage))

    async def search_facts(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]:
        """Return the scope's facts most relevant to ``query``, without an LLM call."""
        return await self._run(self._search_facts(scope, query, limit))

    async def entity(self, scope: GraphScope, name: str) -> _GraphEntity | None:
        """Return the scope's entity named ``name`` in any letter case, else the best fuzzy match, with its facts."""
        return await self._run(self._entity(scope, name))

    async def _add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        # The client installs and imports Graphiti first, so the imports below find it loaded.
        graphiti = await self._graphiti_client()
        from graphiti_core.nodes import EpisodeType  # noqa: PLC0415

        with usage_target(usage):
            await graphiti.add_episode(
                name=episode.name,
                episode_body=episode.body,
                source_description=episode.source_description,
                reference_time=episode.reference_time,
                source=EpisodeType.message if episode.kind == "conversation" else EpisodeType.text,
                group_id=scope.graph_name,
                # Graphiti otherwise adds the graph's ten newest episodes, from any conversation, to every prompt,
                # which multiplies extraction cost and overflows smaller context windows; an episode carries its own context.
                previous_episode_uuids=[],
                entity_types=ENTITY_TYPES,
                excluded_entity_types=EXCLUDED_ENTITY_TYPES,
                custom_extraction_instructions=EXTRACTION_INSTRUCTIONS,
            )

    async def _search_facts(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]:
        graphiti = await self._graphiti_client()
        from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF  # noqa: PLC0415

        results = await graphiti.search_(
            query,
            config=EDGE_HYBRID_SEARCH_RRF.model_copy(update={"limit": limit}),
            group_ids=[scope.graph_name],
        )
        return await _facts(graphiti.driver.clone(database=scope.graph_name), results.edges)

    async def _entity(self, scope: GraphScope, name: str) -> _GraphEntity | None:
        graphiti = await self._graphiti_client()
        from graphiti_core.edges import EntityEdge  # noqa: PLC0415
        from graphiti_core.nodes import EntityNode  # noqa: PLC0415
        from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF  # noqa: PLC0415

        driver = graphiti.driver.clone(database=scope.graph_name)
        # Hybrid search ranks an entity whose summary or longer name mentions the text above one named exactly that
        # (a bare first name), so an exact name is looked up first.
        records, _, _ = await driver.execute_query(_EXACT_ENTITY_QUERY, name=name)
        others: list[str] = []
        if records:
            node = await EntityNode.get_by_uuid(driver, records[0]["uuid"])
        else:
            results = await graphiti.search_(
                name,
                config=NODE_HYBRID_SEARCH_RRF.model_copy(update={"limit": _ENTITY_CANDIDATES}),
                group_ids=[scope.graph_name],
            )
            if not results.nodes:
                return None
            node = results.nodes[0]
            others = [other.name for other in results.nodes[1:]]
        edges = await EntityEdge.get_by_node_uuid(driver, node.uuid)
        # Current facts first, then the most recently valid.
        edges.sort(key=lambda edge: (edge.invalid_at is not None, -(edge.valid_at or edge.created_at).timestamp()))
        return _GraphEntity(
            name=node.name,
            summary=node.summary,
            facts=await _facts(driver, edges[:_MAX_ENTITY_FACTS]),
            others=others,
        )

    async def close(self) -> None:
        """Close the client, stop the embedded database server, and stop the graph loop."""
        graph_loop, self._graph_loop = self._graph_loop, None
        if graph_loop is None:
            return
        try:
            await run_on_loop(graph_loop.loop, self._close_on_graph_loop())
        finally:
            await graph_loop.stop()

    async def _close_on_graph_loop(self) -> None:
        assert self._lock is not None, "the client is only used on the graph loop"
        async with self._lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        graphiti, server = self._graphiti, self._embedded_server
        self._graphiti, self._embedded_server, self._signature = None, None, None
        if graphiti is not None:
            await graphiti.close()
        if server is not None:
            await server.stop()
