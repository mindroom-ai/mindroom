"""The knowledge_graph tool reads only the caller's own graph and shows when each fact held and where it came from."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.knowledge_graph import KnowledgeGraphTools, _render_fact, with_knowledge_graph
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.knowledge_graph.scope import graph_scope
from mindroom.knowledge_graph.store import (
    GraphFact,
    _EntityRef,
    _FactSource,
    _GraphEntity,
    _MergedEntities,
    set_knowledge_graph_store,
)
from mindroom.tool_system.catalog import TOOL_METADATA
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
FACT_ID = "5f0c2a8e-0000-4000-8000-000000000001"
FACT = GraphFact(
    uuid=FACT_ID,
    fact="Alice leads Project Atlas",
    valid_at=JAN,
    invalid_at=MAR,
    sources=[
        _FactSource(
            description="tool_results: room !r thread $t; calls: get_repo 1a2b",
            observed_at=JAN,
        ),
    ],
    source_count=1,
)


class _DocsIndex:
    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        del query, limit
        return [SearchHit(content="Atlas runbook")]


@dataclass
class _Store:
    queries: list[tuple[str, str, int]] = field(default_factory=list)
    edits: list[tuple[str, ...]] = field(default_factory=list)
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
        if name == "Alice":
            return _GraphEntity(
                uuid="alice-id",
                name="Alice",
                summary="Alice leads Atlas.",
                facts=[FACT],
                # Duplicates under the same name and under an alias of it, which only their ids reach.
                others=[_EntityRef("alice", "stray-alice-id"), _EntityRef("Ali", "ali-id")],
                exact=True,
            )
        if name == "Dan":
            return _GraphEntity(
                uuid="dana-id",
                name="Dana",
                summary="A person.",
                facts=[],
                others=[_EntityRef("Dana Whitfield", "whitfield-id"), _EntityRef("Dana's workspace", "workspace-id")],
                exact=False,
            )
        return None

    async def delete_fact(self, scope: GraphScope, fact_id: str) -> str | None:
        if self.failure is not None:
            raise self.failure
        self.edits.append(("delete_fact", scope.graph_name, fact_id))
        return FACT.fact if fact_id == FACT_ID else None

    async def delete_entity(self, scope: GraphScope, entity_id: str) -> str | None:
        self.edits.append(("delete_entity", scope.graph_name, entity_id))
        return "Alice" if entity_id == "alice-id" else None

    async def merge_entities(self, scope: GraphScope, duplicate_id: str, into_id: str) -> _MergedEntities | None:
        self.edits.append(("merge_entities", scope.graph_name, duplicate_id, into_id))
        if (duplicate_id, into_id) == ("stray-alice-id", "alice-id"):
            return _MergedEntities(duplicate="alice", into="Alice", moved_facts=2)
        return None

    async def update_entity_summary(self, scope: GraphScope, entity_id: str, summary: str) -> str | None:
        self.edits.append(("update_entity_summary", scope.graph_name, entity_id, summary))
        return "Alice" if entity_id == "alice-id" else None


@pytest.fixture
def store() -> Iterator[_Store]:
    """Register a fake store as the runtime's graph store."""
    fake = _Store()
    set_knowledge_graph_store(fake)
    yield fake
    set_knowledge_graph_store(None)


def _config(*, enabled: bool = True, private: bool = False, edits: bool = False) -> Config:
    return Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                knowledge_graph=enabled,
                knowledge_graph_edits=edits,
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

    text = await tools.search_graph("who leads Atlas")

    assert store.queries == [(graph_scope("mind", config, paths, identity).graph_name, "who leads Atlas", 10)]
    # Each fact shows when it held and what evidence supports it.
    assert "Alice leads Project Atlas" in text
    assert "valid 2026-01-05 until 2026-03-09 (superseded)" in text
    assert "tool_results: room !r thread $t; calls: get_repo 1a2b (2026-01-05)" in text
    # Ids are only for agents that may edit the graph.
    assert "id: " not in text


@pytest.mark.asyncio
async def test_knowledge_search_finds_facts_in_the_callers_graph(tmp_path: Path, store: _Store) -> None:
    """``search_knowledge_base`` also returns the caller's graph facts, labelled as graph facts."""
    config = _config(private=True)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    identity = build_requester_execution_identity(agent_name="mind", runtime_paths=paths, requester_id=ALICE)
    knowledge = with_knowledge_graph(None, "mind", config, paths, identity)

    hits = await knowledge.asearch("who leads Atlas")

    assert store.queries == [(graph_scope("mind", config, paths, identity).graph_name, "who leads Atlas", 10)]
    assert [(hit.content, hit.meta_data) for hit in hits] == [(_render_fact(FACT), {"source": "knowledge_graph"})]


@pytest.mark.asyncio
async def test_knowledge_search_keeps_the_agents_other_sources(tmp_path: Path, store: _Store) -> None:
    """The graph joins the agent's knowledge bases instead of replacing them."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    docs = KnowledgeSource(KnowledgeSourceDescription("docs", "Runbooks."), _DocsIndex())
    knowledge = with_knowledge_graph(AgentKnowledge(sources=(docs,)), "mind", _config(), paths, None)

    hits = await knowledge.asearch("who leads Atlas")

    assert [source.description.base_id for source in knowledge.sources] == ["docs", "knowledge_graph"]
    assert [hit.content for hit in hits] == ["Atlas runbook", _render_fact(FACT)]
    assert len(store.queries) == 1


@pytest.mark.asyncio
async def test_knowledge_search_fails_rather_than_finding_nothing_in_a_stopped_graph(tmp_path: Path) -> None:
    """A graph that is not running is a failed source, so a graph-only search never looks empty."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    knowledge = with_knowledge_graph(None, "mind", _config(), paths, None)

    with pytest.raises(RuntimeError, match="not running"):
        await knowledge.asearch("who leads Atlas")


def test_a_fact_with_more_sources_than_shown_says_how_many_more() -> None:
    """A fact re-seen many times lists its newest sources and counts the rest, so lookups stay small."""
    rendered = _render_fact(replace(FACT, source_count=120))

    assert rendered.count("tool_results:") == 1
    assert "and 119 earlier" in rendered


@pytest.mark.asyncio
@pytest.mark.usefixtures("store")
async def test_graph_entity_returns_summary_or_says_unknown(tmp_path: Path) -> None:
    """An entity lookup shows its summary and facts, and says when nothing matches."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    tools = KnowledgeGraphTools("mind", _config(), paths, None)

    assert "Alice leads Atlas." in await tools.graph_entity("Alice")
    assert "No entity matches" in await tools.graph_entity("Nobody")


@pytest.mark.asyncio
@pytest.mark.usefixtures("store")
async def test_graph_entity_shows_other_close_matches(tmp_path: Path) -> None:
    """A fuzzy match names the next closest entities, and an exact match adds nothing."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    tools = KnowledgeGraphTools("mind", _config(), paths, None)

    fuzzy = await tools.graph_entity("Dan")
    exact = await tools.graph_entity("Alice")

    assert fuzzy.endswith("\nOther close matches: Dana Whitfield, Dana's workspace")
    # Without ids a duplicate found under the name asked for cannot be looked up, so it is not offered.
    assert exact == "Alice: Alice leads Atlas.\n" + _render_fact(FACT)
    assert "(id " not in fuzzy + exact
    assert "id: " not in fuzzy + exact


@pytest.mark.asyncio
async def test_unavailable_graph_returns_a_readable_error(tmp_path: Path, store: _Store) -> None:
    """A database failure becomes a message the agent can relay, not an exception."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    store.failure = ConnectionError("socket closed")

    text = await KnowledgeGraphTools("mind", _config(), paths, None).search_graph("Atlas")

    assert text.startswith("The knowledge graph is unavailable")
    # The error can carry request data or internals; the log keeps it, the agent and its requester do not.
    assert "socket closed" not in text


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


def test_graph_edits_are_offered_only_when_allowed(tmp_path: Path) -> None:
    """The edit functions exist only for an agent allowed to edit its graph."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)

    reader = KnowledgeGraphTools("mind", _config(), paths, None)
    editor = KnowledgeGraphTools("mind", _config(edits=True), paths, None)

    assert set(reader.async_functions) == {"search_graph", "graph_entity"}
    assert set(editor.async_functions) == set(TOOL_METADATA["knowledge_graph"].function_names)


@pytest.mark.asyncio
@pytest.mark.usefixtures("store")
async def test_an_editing_agent_sees_the_ids_it_edits_by(tmp_path: Path) -> None:
    """Facts, entities, and other matches, including a duplicate of the same name, show their ids."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    tools = KnowledgeGraphTools("mind", _config(edits=True), paths, None)

    found = await tools.search_graph("who leads Atlas")
    exact = await tools.graph_entity("Alice")
    fuzzy = await tools.graph_entity("Dan")

    assert f"- Alice leads Project Atlas\n  id: {FACT_ID}\n" in found
    assert exact.startswith("Alice (id alice-id): Alice leads Atlas.\n")
    assert f"id: {FACT_ID}" in exact
    # Entities found under the name asked for are duplicates to merge, not close matches.
    assert exact.endswith("\nOther entities under this name: alice (id stray-alice-id), Ali (id ali-id)")
    assert fuzzy.endswith("\nOther close matches: Dana Whitfield (id whitfield-id), Dana's workspace (id workspace-id)")


@pytest.mark.asyncio
async def test_graph_edits_change_only_the_callers_graph(tmp_path: Path, store: _Store) -> None:
    """Each edit goes to the requester's own graph and reports what it changed or that the id is unknown."""
    config = _config(private=True, edits=True)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    identity = build_requester_execution_identity(agent_name="mind", runtime_paths=paths, requester_id=ALICE)
    graph = graph_scope("mind", config, paths, identity).graph_name
    tools = KnowledgeGraphTools("mind", config, paths, identity)

    assert await tools.delete_graph_fact(FACT_ID) == "Deleted the fact 'Alice leads Project Atlas'."
    assert await tools.delete_graph_fact("nope") == "No fact in your graph has id 'nope'."
    assert await tools.delete_graph_entity("alice-id") == "Deleted 'Alice' and every fact that touched it."
    assert await tools.delete_graph_entity("nope") == "No entity in your graph has id 'nope'."
    assert (
        await tools.merge_graph_entities("stray-alice-id", "alice-id") == "Merged 'alice' into 'Alice', moving 2 facts."
    )
    assert (
        await tools.merge_graph_entities("nope", "alice-id")
        == "'nope' or 'alice-id' is not the id of an entity in your graph."
    )
    assert (
        await tools.update_graph_entity_summary("alice-id", "Alice leads Atlas.") == "Updated the summary of 'Alice'."
    )
    assert await tools.update_graph_entity_summary("nope", "x") == "No entity in your graph has id 'nope'."

    assert store.edits == [
        ("delete_fact", graph, FACT_ID),
        ("delete_fact", graph, "nope"),
        ("delete_entity", graph, "alice-id"),
        ("delete_entity", graph, "nope"),
        ("merge_entities", graph, "stray-alice-id", "alice-id"),
        ("merge_entities", graph, "nope", "alice-id"),
        ("update_entity_summary", graph, "alice-id", "Alice leads Atlas."),
        ("update_entity_summary", graph, "nope", "x"),
    ]


@pytest.mark.asyncio
async def test_an_entity_is_not_merged_into_itself(tmp_path: Path, store: _Store) -> None:
    """Merging an entity into itself would delete it, so the store is never asked."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    tools = KnowledgeGraphTools("mind", _config(edits=True), paths, None)

    assert await tools.merge_graph_entities("alice-id", "alice-id") == "An entity cannot be merged into itself."
    assert store.edits == []


@pytest.mark.asyncio
async def test_a_failed_edit_returns_a_readable_error(tmp_path: Path, store: _Store) -> None:
    """A database failure during an edit becomes a message the agent can relay, as for a lookup."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    store.failure = ConnectionError("socket closed")

    text = await KnowledgeGraphTools("mind", _config(edits=True), paths, None).delete_graph_fact(FACT_ID)

    assert text == "The knowledge graph is unavailable right now; try again later."
