"""The Graphiti store over embedded FalkorDB Lite: one graph per scope, facts with validity and sources."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from falkordb.asyncio.graph import AsyncGraph
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from redis.asyncio import Redis

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge_graph import store as store_module
from mindroom.knowledge_graph.scope import GraphScope
from mindroom.knowledge_graph.store import EpisodeInput, GraphitiStore, _FactSource
from mindroom.knowledge_graph.usage import UsageTarget

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from falkordb.query_result import QueryResult

ALPHA = GraphScope(agent_name="mind", execution_identity=None, graph_name="kg_" + "a" * 24)
BETA = GraphScope(agent_name="mind", execution_identity=None, graph_name="kg_" + "b" * 24)
JAN = datetime(2026, 1, 5, tzinfo=UTC)
MAR = datetime(2026, 3, 9, tzinfo=UTC)


class _HashEmbedder:
    """Deterministic bag-of-words vectors, so related texts land near each other without a model."""

    async def async_get_embedding(self, text: str) -> list[float]:
        vector = [0.0] * 32
        for word in text.lower().split():
            vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 32] += 1.0
        return vector


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[GraphitiStore]:
    """A store over a fresh embedded database."""
    config = Config(
        agents={"mind": AgentConfig(display_name="Mind", knowledge_graph=True)},
        models={"luna": ModelConfig(provider="openai", id="gpt-6-luna")},
        knowledge_graph={"model": "luna"},
    )
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    with patch.object(store_module, "create_configured_embedder", return_value=_HashEmbedder()):
        graph_store = GraphitiStore(lambda: config, paths)
        try:
            yield graph_store
        finally:
            await graph_store.close()


async def _seed_fact(
    graph_store: GraphitiStore,
    scope: GraphScope,
    *,
    fact: str,
    source_description: str,
    valid_at: datetime,
    invalid_at: datetime | None = None,
    seen: int = 1,
) -> None:
    """Store a fact supported by ``seen`` episodes, one day apart from ``valid_at``, numbered in their descriptions."""
    graphiti = await graph_store._graphiti_client()
    driver = graphiti.driver.clone(database=scope.graph_name)
    embedder = graphiti.embedder
    episodes = [
        EpisodicNode(
            name="episode",
            group_id=scope.graph_name,
            source=EpisodeType.text,
            source_description=source_description if seen == 1 else f"{source_description} #{index}",
            content=fact,
            valid_at=valid_at + timedelta(days=index),
        )
        for index in range(seen)
    ]
    for episode in episodes:
        await episode.save(driver)
    alice = EntityNode(name="Alice", group_id=scope.graph_name, summary="Alice leads Project Atlas.")
    atlas = EntityNode(name="Project Atlas", group_id=scope.graph_name, summary="A data platform project.")
    for node in (alice, atlas):
        await node.generate_name_embedding(embedder)
        await node.save(driver)
    edge = EntityEdge(
        source_node_uuid=alice.uuid,
        target_node_uuid=atlas.uuid,
        name="LEADS",
        fact=fact,
        group_id=scope.graph_name,
        episodes=[episode.uuid for episode in episodes],
        created_at=valid_at,
        valid_at=valid_at,
        invalid_at=invalid_at,
    )
    await edge.generate_embedding(embedder)
    await edge.save(driver)


@pytest.mark.asyncio
async def test_search_facts_returns_validity_and_sources(store: GraphitiStore) -> None:
    """A fact comes back with when it held and which episode supports it."""
    await _seed_fact(
        store,
        ALPHA,
        fact="Alice leads Project Atlas",
        source_description="conversation: room !r:x thread $t",
        valid_at=JAN,
        invalid_at=MAR,
    )

    facts = await store.search_facts(ALPHA, "who leads Project Atlas", limit=5)

    assert [fact.fact for fact in facts] == ["Alice leads Project Atlas"]
    assert facts[0].valid_at == JAN
    assert facts[0].invalid_at == MAR
    assert facts[0].sources == [
        _FactSource(description="conversation: room !r:x thread $t", observed_at=JAN),
    ]
    # Each scope is its own graph, so a search in one never returns another's facts.
    assert await store.search_facts(BETA, "Alice Project Atlas", limit=5) == []


@pytest.mark.asyncio
async def test_a_fact_seen_many_times_returns_only_its_newest_sources(store: GraphitiStore) -> None:
    """A fact re-seen on every exploration keeps every source, but a lookup loads and returns only the newest few."""
    await _seed_fact(
        store,
        ALPHA,
        fact="Alice leads Project Atlas",
        source_description="tool_results",
        valid_at=JAN,
        seen=8,
    )

    [fact] = await store.search_facts(ALPHA, "who leads Project Atlas", limit=5)

    assert [source.description for source in fact.sources] == ["tool_results #5", "tool_results #6", "tool_results #7"]
    assert fact.source_count == 8


@pytest.mark.asyncio
async def test_entity_returns_summary_and_facts(store: GraphitiStore) -> None:
    """An entity lookup returns its summary and the facts that touch it."""
    await _seed_fact(
        store,
        ALPHA,
        fact="Alice leads Project Atlas",
        source_description="tool_results: room !r thread $t; calls: github.get_repo 1a2b",
        valid_at=JAN,
    )

    entity = await store.entity(ALPHA, "Alice")

    assert entity is not None
    assert entity.name == "Alice"
    assert entity.summary == "Alice leads Project Atlas."
    assert [fact.fact for fact in entity.facts] == ["Alice leads Project Atlas"]
    assert entity.facts[0].sources[0].description.startswith("tool_results:")
    assert await store.entity(BETA, "Alice") is None


def _holds_embedding(value: object) -> bool:
    """Whether a FalkorDB reply value is, or contains, one of the test embedder's 32-number vectors."""
    if isinstance(value, dict):
        return any(_holds_embedding(item) for item in value.values())
    if isinstance(value, list):
        if len(value) == 32 and all(isinstance(item, float) for item in value):
            return True
        return any(_holds_embedding(item) for item in value)
    return False


@pytest.mark.asyncio
async def test_lookups_do_not_read_embeddings_back(store: GraphitiStore) -> None:
    """Graphiti drops the embeddings its searches read, and parsing them stalled the event loop for every search."""
    await _seed_fact(store, ALPHA, fact="Alice leads Project Atlas", source_description="conversation: a", valid_at=JAN)
    rows: list[bool] = []
    query = AsyncGraph.query

    async def recording_query(graph: AsyncGraph, *args: object, **kwargs: object) -> QueryResult:
        result = await query(graph, *args, **kwargs)
        # Checked on arrival, because Graphiti pops the embeddings out of these rows in place.
        rows.extend(_holds_embedding(row) for row in result.result_set)
        return result

    with patch.object(AsyncGraph, "query", recording_query):
        facts = await store.search_facts(ALPHA, "who leads Project Atlas", limit=5)
        entity = await store.entity(ALPHA, "Alice")

    assert [fact.fact for fact in facts] == ["Alice leads Project Atlas"]
    assert entity is not None
    assert [fact.fact for fact in entity.facts] == ["Alice leads Project Atlas"]
    assert rows
    assert not any(rows)


def test_importing_graphiti_leaves_the_environment_alone(tmp_path: Path) -> None:
    """Graphiti loads a .env at import; MindRoom keeps that from changing its process environment."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("KG_IMPORT_PROBE=leaked\n", encoding="utf-8")
    script = f"""
import os
from unittest.mock import patch
import dotenv.main
with patch.object(dotenv.main, "find_dotenv", return_value={str(dotenv_file)!r}):
    from mindroom.knowledge_graph.store import _import_graphiti
    _import_graphiti()
assert "KG_IMPORT_PROBE" not in os.environ, "Graphiti's .env reached os.environ"
assert "PYTHON_DOTENV_DISABLED" not in os.environ
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_graph_operations_install_graphiti_before_importing_it() -> None:
    """Every operation reaches the installing, guarded client before it imports anything from Graphiti."""
    script = """
import asyncio
import sys
from mindroom.knowledge_graph.store import GraphitiStore

class ClientReached(Exception):
    pass

async def client(self):
    raise ClientReached

GraphitiStore._graphiti_client = client
store = GraphitiStore.__new__(GraphitiStore)
for call in (store.search_facts(None, "q", 1), store.entity(None, "n"), store.add_episode(None, None, None)):
    try:
        asyncio.run(call)
    except ClientReached:
        pass
assert "graphiti_core" not in sys.modules, "Graphiti was imported before its client was built"
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_a_crashed_database_server_is_started_again(store: GraphitiStore) -> None:
    """A database server that died is replaced on the next lookup instead of failing until MindRoom restarts."""
    await _seed_fact(store, ALPHA, fact="Alice leads Project Atlas", source_description="conversation: a", valid_at=JAN)
    server = store._embedded_server
    assert server is not None
    # A kill can lose the last second of writes, which is documented and not what this test checks.
    # WAITAOF waits only for this connection's own writes; the append-only file is written in order, so one write
    # here that reaches disk means the seeded fact did too.
    client = Redis(unix_socket_path=server.socket_path)
    try:
        await client.set("fsync-barrier", "1")
        assert await client.execute_command("WAITAOF", 1, 0, 30000) == [1, 0]
    finally:
        await client.aclose()
    server.process.kill()
    await server.process.wait()

    facts = await store.search_facts(ALPHA, "Alice Project Atlas", limit=5)

    assert [fact.fact for fact in facts] == ["Alice leads Project Atlas"]


@pytest.mark.asyncio
async def test_the_embedder_is_built_off_the_event_loop(store: GraphitiStore) -> None:
    """A local embedder installs packages and loads a model when built, which must not stall every agent's replies."""
    built_on: list[threading.Thread] = []

    def embedder(*_args: object) -> _HashEmbedder:
        built_on.append(threading.current_thread())
        return _HashEmbedder()

    with patch.object(store_module, "create_configured_embedder", embedder):
        await store.search_facts(ALPHA, "anything", limit=1)

    assert built_on
    assert built_on[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_extraction_sends_only_the_episode_itself(store: GraphitiStore) -> None:
    """Graphiti otherwise adds the graph's last ten episodes, from any conversation, to every extraction prompt."""
    graphiti = await store._graphiti_client()
    episode = EpisodeInput("conversation", "conversation", "person @alice (x): hi", "conversation: room !r", JAN)

    with patch.object(graphiti, "add_episode", AsyncMock()) as add:
        await store.add_episode(ALPHA, episode, UsageTarget("mind", "!r:$t", None))

    assert add.await_args is not None
    assert add.await_args.kwargs["previous_episode_uuids"] == []
