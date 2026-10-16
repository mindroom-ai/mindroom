"""The knowledge_graph tool reads only the caller's own graph and shows when each fact held and where it came from."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.knowledge_graph import KnowledgeGraphTools
from mindroom.knowledge_graph.scope import graph_scope
from mindroom.knowledge_graph.store import GraphFact, _FactSource, _GraphEntity, set_knowledge_graph_store
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.knowledge_graph.scope import GraphScope
    from mindroom.knowledge_graph.store import EpisodeInput
    from mindroom.knowledge_graph.usage import UsageTarget

ALICE = "@alice:example.test"
JAN = datetime(2026, 1, 5, tzinfo=UTC)
MAR = datetime(2026, 3, 9, tzinfo=UTC)
FACT = GraphFact(
    fact="Alice leads Project Atlas",
    valid_at=JAN,
    invalid_at=MAR,
    sources=[
        _FactSource(
            kind="tool_results",
            description="tool_results: room !r thread $t; calls: get_repo 1a2b",
            observed_at=JAN,
        ),
    ],
)


@dataclass
class _Store:
    queries: list[tuple[str, str, int]] = field(default_factory=list)
    failure: Exception | None = None

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        del scope, episode, usage
        raise AssertionError

    async def search_facts(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]:
        if self.failure is not None:
            raise self.failure
        self.queries.append((scope.graph_name, query, limit))
        return [FACT]

    async def entity(self, scope: GraphScope, name: str) -> _GraphEntity | None:
        self.queries.append((scope.graph_name, name, 0))
        return _GraphEntity(name="Alice", summary="Alice leads Atlas.", facts=[FACT]) if name == "Alice" else None

    async def close(self) -> None:
        return None


@pytest.fixture
def store() -> Iterator[_Store]:
    """Register a fake store as the runtime's graph store."""
    fake = _Store()
    set_knowledge_graph_store(fake)
    yield fake
    set_knowledge_graph_store(None)


def _config(*, enabled: bool = True, private: bool = False) -> Config:
    return Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                knowledge_graph=enabled,
                private=AgentPrivateConfig(per="user") if private else None,
            ),
        },
        models={"luna": ModelConfig(provider="openai", id="gpt-6-luna")},
        knowledge_graph={"model": "luna"},
    )


@pytest.mark.asyncio
async def test_search_graph_reads_only_the_callers_graph(tmp_path: Path, store: _Store) -> None:
    """A private agent's requester searches their own graph, chosen from their identity."""
    config = _config(private=True)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    identity = build_requester_execution_identity(agent_name="mind", runtime_paths=paths, requester_id=ALICE)
    tools = KnowledgeGraphTools("mind", config, paths, identity)

    await tools.search_graph("who leads Atlas")

    assert store.queries == [(graph_scope("mind", config, paths, identity).graph_name, "who leads Atlas", 10)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("store")
async def test_search_graph_shows_validity_and_sources(tmp_path: Path) -> None:
    """Each fact shows when it held and what evidence supports it."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    text = await KnowledgeGraphTools("mind", _config(), paths, None).search_graph("Atlas")

    assert "Alice leads Project Atlas" in text
    assert "valid 2026-01-05 until 2026-03-09 (superseded)" in text
    assert "tool_results: room !r thread $t; calls: get_repo 1a2b (2026-01-05)" in text


@pytest.mark.asyncio
@pytest.mark.usefixtures("store")
async def test_graph_entity_returns_summary_or_says_unknown(tmp_path: Path) -> None:
    """An entity lookup shows its summary and facts, and says when nothing matches."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    tools = KnowledgeGraphTools("mind", _config(), paths, None)

    assert "Alice leads Atlas." in await tools.graph_entity("Alice")
    assert "No entity matches" in await tools.graph_entity("Nobody")


@pytest.mark.asyncio
async def test_unavailable_graph_returns_a_readable_error(tmp_path: Path, store: _Store) -> None:
    """A database failure becomes a message the agent can relay, not an exception."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    store.failure = ConnectionError("socket closed")

    text = await KnowledgeGraphTools("mind", _config(), paths, None).search_graph("Atlas")

    assert text.startswith("The knowledge graph is unavailable")


@pytest.mark.asyncio
async def test_search_limit_is_clamped(tmp_path: Path, store: _Store) -> None:
    """Limits stay between 1 and 50."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    tools = KnowledgeGraphTools("mind", _config(), paths, None)

    await tools.search_graph("a", limit=0)
    await tools.search_graph("b", limit=500)

    assert [limit for _, _, limit in store.queries] == [1, 50]


def test_graph_enabled_agents_are_offered_the_tool() -> None:
    """Enabling the graph gives the agent its tool; disabling removes it."""

    def offered(config: Config) -> set[str]:
        return {entry.name for entry in visible_tool_surface(agent_name="mind", config=config).runtime_tool_configs}

    assert "knowledge_graph" in offered(_config())
    assert "knowledge_graph" not in offered(_config(enabled=False))
