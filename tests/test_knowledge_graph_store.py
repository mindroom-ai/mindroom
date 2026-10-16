"""The Graphiti store over embedded FalkorDB Lite: one graph per scope, facts with validity and sources."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import pytest_asyncio
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge_graph import store as store_module
from mindroom.knowledge_graph.scope import GraphScope
from mindroom.knowledge_graph.store import GraphitiStore, _FactSource

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

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
) -> None:
    graphiti = await graph_store._graphiti_client()
    driver = graphiti.driver.clone(database=scope.graph_name)
    embedder = graphiti.embedder
    episode = EpisodicNode(
        name="episode",
        group_id=scope.graph_name,
        source=EpisodeType.text,
        source_description=source_description,
        content=fact,
        valid_at=valid_at,
    )
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
        episodes=[episode.uuid],
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


@pytest.mark.asyncio
async def test_scopes_never_see_each_others_facts(store: GraphitiStore) -> None:
    """Each scope is its own graph, so a search in one never returns another's facts."""
    await _seed_fact(store, ALPHA, fact="Alice leads Project Atlas", source_description="conversation: a", valid_at=JAN)

    assert await store.search_facts(BETA, "Alice Project Atlas", limit=5) == []
    assert await store.entity(BETA, "Alice") is None


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
