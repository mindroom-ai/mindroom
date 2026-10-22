"""The Graphiti store over embedded FalkorDB Lite: one graph per scope, facts with validity and sources."""

from __future__ import annotations

import asyncio
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
from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF
from redis.asyncio import Redis

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge_graph import store as store_module
from mindroom.knowledge_graph.clients import MindRoomLLMClient
from mindroom.knowledge_graph.extraction import ENTITY_TYPES, extraction_instructions
from mindroom.knowledge_graph.names import KnownEntity
from mindroom.knowledge_graph.scope import GraphScope
from mindroom.knowledge_graph.store import EpisodeInput, GraphitiStore, _FactSource
from mindroom.knowledge_graph.usage import UsageTarget, current_usage_target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from falkordb.query_result import QueryResult
    from graphiti_core.prompts.models import Message
    from pydantic import BaseModel

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
    """A store over a fresh embedded database, with one alias entry."""
    config = Config(
        agents={"mind": AgentConfig(display_name="Mind", knowledge_graph=True)},
        models={"luna": ModelConfig(provider="openai", id="gpt-6-luna")},
        knowledge_graph={"model": "luna", "aliases": {"Robin Vale": ["@robin:example.test", "Robin"]}},
    )
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    with patch.object(store_module, "create_configured_embedder", return_value=_HashEmbedder()):
        graph_store = GraphitiStore(lambda: config, paths, asyncio.get_running_loop())
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

    # Graphiti's objects belong to the store's graph loop, so the seeding runs there.
    async def seed() -> None:
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

    await graph_store._run(seed())


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
@pytest.mark.parametrize(
    "query",
    [
        "who leads Project Atlas _ now",
        "\x1b[31mwho leads Project Atlas\x1b[0m",
        "who leads \x07 Project \x7f Atlas",
    ],
)
async def test_search_survives_text_redisearch_cannot_parse(store: GraphitiStore, query: str) -> None:
    """A lone underscore or a control character, as in a tool's colored output, reaches no fulltext query."""
    await _seed_fact(
        store,
        ALPHA,
        fact="Alice leads Project Atlas",
        source_description="tool_results: room !r:x thread $t",
        valid_at=JAN,
    )

    facts = await store.search_facts(ALPHA, query, limit=5)

    assert [fact.fact for fact in facts] == ["Alice leads Project Atlas"]


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


async def _seed_entity(
    graph_store: GraphitiStore,
    scope: GraphScope,
    name: str,
    *,
    summary: str = "",
    facts: int = 0,
    name_embedding: list[float] | None = None,
    created_at: datetime | None = None,
) -> None:
    """Store an entity with ``facts`` edges to other new entities, and optionally a chosen name embedding and age."""

    async def seed() -> None:
        graphiti = await graph_store._graphiti_client()
        driver = graphiti.driver.clone(database=scope.graph_name)
        node = EntityNode(name=name, group_id=scope.graph_name, summary=summary)
        if created_at is not None:
            node.created_at = created_at
        if name_embedding is None:
            await node.generate_name_embedding(graphiti.embedder)
        else:
            node.name_embedding = name_embedding
        await node.save(driver)
        for index in range(facts):
            other = EntityNode(name=f"Counterpart {index}", group_id=scope.graph_name)
            await other.generate_name_embedding(graphiti.embedder)
            await other.save(driver)
            edge = EntityEdge(
                source_node_uuid=node.uuid,
                target_node_uuid=other.uuid,
                name="RELATES",
                fact=f"{name} fact {index}",
                group_id=scope.graph_name,
                episodes=[],
                created_at=JAN,
                valid_at=JAN,
            )
            await edge.generate_embedding(graphiti.embedder)
            await edge.save(driver)

    await graph_store._run(seed())


async def _search_ranking(graph_store: GraphitiStore, scope: GraphScope, query: str) -> list[str]:
    """The names Graphiti's hybrid search ranks for ``query``, best first, as a lookup without an exact name sees them."""

    async def search() -> list[str]:
        graphiti = await graph_store._graphiti_client()
        results = await graphiti.search_(
            query,
            config=NODE_HYBRID_SEARCH_RRF.model_copy(update={"limit": 5}),
            group_ids=[scope.graph_name],
        )
        return [node.name for node in results.nodes]

    return await graph_store._run(search())


@pytest.mark.asyncio
async def test_entity_prefers_an_exact_name_over_a_better_search_hit(store: GraphitiStore) -> None:
    """A name that matches an entity exactly finds it, even where search ranks a longer name higher."""
    await _seed_entity(
        store,
        ALPHA,
        "Dana - Brokerage",
        summary="Dana dana dana brokerage account",
    )
    # The exact entity's name embedding points away from the query, as a real embedding can for a bare first name.
    await _seed_entity(store, ALPHA, "Dana", summary="A person.", facts=3, name_embedding=[1.0] * 32)
    assert (await _search_ranking(store, ALPHA, "dana"))[0] == "Dana - Brokerage"

    entity = await store.entity(ALPHA, "dana")

    assert entity is not None
    assert entity.name == "Dana"
    assert entity.summary == "A person."
    assert len(entity.facts) == 3
    assert entity.others == []


@pytest.mark.asyncio
async def test_entity_with_several_exact_names_picks_the_one_with_most_facts(store: GraphitiStore) -> None:
    """Names that differ only in case are all exact matches, and the entity with the most facts wins."""
    await _seed_entity(store, ALPHA, "dana", summary="A stray duplicate.", facts=1)
    await _seed_entity(store, ALPHA, "Dana", summary="A person.", facts=3)
    await _seed_entity(store, ALPHA, "DANA", summary="Another duplicate.")

    entity = await store.entity(ALPHA, "Dana")

    assert entity is not None
    assert entity.summary == "A person."
    assert entity.others == []


@pytest.mark.asyncio
async def test_entity_with_equally_many_facts_picks_the_oldest(store: GraphitiStore) -> None:
    """Exact names with the same number of facts resolve to the oldest entity, whatever order they were stored in."""
    await _seed_entity(store, ALPHA, "DANA", summary="Stored first, created later.", facts=2, created_at=MAR)
    await _seed_entity(store, ALPHA, "dana", summary="Stored second, created earlier.", facts=2, created_at=JAN)
    await _seed_entity(store, BETA, "DANA", summary="Stored first, created earlier.", facts=2, created_at=JAN)
    await _seed_entity(store, BETA, "dana", summary="Stored second, created later.", facts=2, created_at=MAR)

    stored_second = await store.entity(ALPHA, "Dana")
    stored_first = await store.entity(BETA, "Dana")

    assert stored_second is not None
    assert stored_second.summary == "Stored second, created earlier."
    assert stored_first is not None
    assert stored_first.summary == "Stored first, created earlier."


@pytest.mark.asyncio
async def test_entity_without_exact_match_lists_other_close_matches(store: GraphitiStore) -> None:
    """With no exact name, the best search hit is the entity and the next hits are listed as other close matches."""
    for name in ("Atlas dashboard v2", "Atlas Labs", "Project dashboard"):
        await _seed_entity(store, ALPHA, name, summary=f"{name} summary.")

    entity = await store.entity(ALPHA, "Atlas dashboard")

    assert entity is not None
    ranking = await _search_ranking(store, ALPHA, "Atlas dashboard")
    assert ranking[0] == "Atlas dashboard v2"
    assert sorted(ranking) == ["Atlas Labs", "Atlas dashboard v2", "Project dashboard"]
    assert [entity.name, *entity.others] == ranking


@pytest.mark.asyncio
async def test_entity_looks_up_the_one_name_an_alias_or_id_stands_for(store: GraphitiStore) -> None:
    """A configured alias, Matrix ID, or agent key finds the entity under its one name, ahead of one named the alias."""
    await _seed_entity(store, ALPHA, "Robin", summary="A stray duplicate.")
    await _seed_entity(store, ALPHA, "Robin Vale", summary="A person.", facts=2)
    await _seed_entity(store, ALPHA, "Mind", summary="The agent.")

    for name in ("robin", "@robin:example.test", "ROBIN VALE"):
        entity = await store.entity(ALPHA, name)
        assert entity is not None
        assert entity.name == "Robin Vale"
    mind = await store.entity(ALPHA, "mind")
    assert mind is not None
    assert mind.summary == "The agent."


@pytest.mark.asyncio
async def test_entity_falls_back_to_the_name_asked_for(store: GraphitiStore) -> None:
    """A graph extracted before an alias entry existed can hold only the alias, which is then found as asked."""
    await _seed_entity(store, ALPHA, "Robin Vale Consulting", summary="A company.")
    await _seed_entity(store, ALPHA, "Robin", summary="A person.", name_embedding=[1.0] * 32)

    entity = await store.entity(ALPHA, "Robin")

    assert entity is not None
    assert entity.summary == "A person."


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
        # An exact name loads the entity by its uuid; any other text goes through Graphiti's node search.
        exact = await store.entity(ALPHA, "Alice")
        before_fuzzy = len(rows)
        fuzzy = await store.entity(ALPHA, "Atlas")

    assert [fact.fact for fact in facts] == ["Alice leads Project Atlas"]
    assert exact is not None
    assert [fact.fact for fact in exact.facts] == ["Alice leads Project Atlas"]
    assert fuzzy is not None
    assert fuzzy.name == "Project Atlas"
    assert before_fuzzy
    assert len(rows) > before_fuzzy
    assert not any(rows)


@pytest.mark.asyncio
async def test_graph_work_does_not_hold_the_event_loop(store: GraphitiStore) -> None:
    """A query waits for a task on the caller's loop, which can only run if Graphiti works on another loop."""
    await _seed_fact(store, ALPHA, fact="Alice leads Project Atlas", source_description="conversation: a", valid_at=JAN)
    querying = threading.Event()
    released = threading.Event()
    ran_on: list[threading.Thread] = []
    query = AsyncGraph.query

    async def waiting_query(graph: AsyncGraph, *args: object, **kwargs: object) -> QueryResult:
        ran_on.append(threading.current_thread())
        querying.set()
        assert released.wait(timeout=10), "the caller's loop never ran while Graphiti queried"
        return await query(graph, *args, **kwargs)

    async def release() -> None:
        # Waiting in a worker thread keeps this loop free, so only a query on this loop could block the release.
        assert await asyncio.to_thread(querying.wait, 10)
        released.set()

    with patch.object(AsyncGraph, "query", waiting_query):
        facts, _ = await asyncio.gather(store.search_facts(ALPHA, "who leads Project Atlas", limit=5), release())

    assert [fact.fact for fact in facts] == ["Alice leads Project Atlas"]
    assert ran_on
    assert threading.main_thread() not in ran_on


@pytest.mark.asyncio
async def test_a_cancelled_lookup_still_lets_the_store_close(store: GraphitiStore) -> None:
    """Cancelling a caller cancels its graph work, and closing afterwards stops the graph thread."""
    await _seed_fact(store, ALPHA, fact="Alice leads Project Atlas", source_description="conversation: a", valid_at=JAN)
    started = threading.Event()
    cancelled = threading.Event()
    query = AsyncGraph.query

    async def slow_query(graph: AsyncGraph, cypher: str, *args: object, **kwargs: object) -> QueryResult:
        # The driver also builds its indexes in background tasks; only the lookup's own queries hang.
        if not cypher.lstrip().startswith("CREATE"):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return await query(graph, cypher, *args, **kwargs)

    with patch.object(AsyncGraph, "query", slow_query):
        lookup = asyncio.create_task(store.search_facts(ALPHA, "Alice", limit=5))
        assert await asyncio.to_thread(started.wait, 10)
        lookup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lookup
        # Checked before closing, which would cancel the graph work anyway.
        assert await asyncio.to_thread(cancelled.wait, 10)
    await store.close()

    assert not any(thread.name == "mindroom-knowledge-graph" for thread in threading.enumerate())


@pytest.mark.asyncio
async def test_closing_the_store_fails_a_lookup_it_interrupts(store: GraphitiStore) -> None:
    """A lookup cut short by closing the store fails like an outage, which the tool reports as unavailable."""
    await _seed_fact(store, ALPHA, fact="Alice leads Project Atlas", source_description="conversation: a", valid_at=JAN)
    started = threading.Event()
    query = AsyncGraph.query

    async def slow_query(graph: AsyncGraph, cypher: str, *args: object, **kwargs: object) -> QueryResult:
        # The driver also builds its indexes in background tasks; only the lookup's own queries hang.
        if not cypher.lstrip().startswith("CREATE"):
            started.set()
            await asyncio.sleep(30)
        return await query(graph, cypher, *args, **kwargs)

    with patch.object(AsyncGraph, "query", slow_query):
        lookup = asyncio.create_task(store.search_facts(ALPHA, "Alice", limit=5))
        assert await asyncio.to_thread(started.wait, 10)
        await store.close()
        with pytest.raises(RuntimeError, match="cancelled on the other event loop"):
            await lookup


@pytest.mark.asyncio
async def test_extraction_goes_to_the_owners_loop_whoever_calls_first(store: GraphitiStore) -> None:
    """Extraction runs on the loop that created the store, even when a short-lived loop makes the first call."""
    await asyncio.to_thread(asyncio.run, store.search_facts(ALPHA, "anything", limit=1))

    graphiti = await store._run(store._graphiti_client())

    assert graphiti.llm_client._runtime_loop is asyncio.get_running_loop()


@pytest.mark.asyncio
async def test_closing_an_unused_store_starts_no_thread(tmp_path: Path) -> None:
    """A runtime whose store never ran anything does not start the graph thread to close it."""
    config = Config(
        agents={"mind": AgentConfig(display_name="Mind", knowledge_graph=True)},
        models={"luna": ModelConfig(provider="openai", id="gpt-6-luna")},
        knowledge_graph={"model": "luna"},
    )
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    graph_store = GraphitiStore(lambda: config, paths, asyncio.get_running_loop())

    await graph_store.close()

    assert not any(thread.name == "mindroom-knowledge-graph" for thread in threading.enumerate())


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
store = GraphitiStore(None, None, None)
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
    await store._run(server.process.wait())

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
    """Graphiti otherwise adds the graph's last ten episodes, from any conversation, to every extraction prompt.

    The episode is extracted into the entity types, with the generic type excluded and the instructions added: the
    built-in ones, the configured ones, and the names of the agents and people the episode knows.
    """
    store._config_provider().knowledge_graph.extraction_instructions = "Focus on lab equipment."
    graphiti = await store._run(store._graphiti_client())
    known = (KnownEntity("alice", "person", ("@alice:x",)),)
    episode = EpisodeInput("conversation", "conversation", "alice: hi", "conversation: room !r", JAN, known)

    with patch.object(graphiti, "add_episode", AsyncMock()) as add:
        await store.add_episode(ALPHA, episode, UsageTarget("mind", "!r:$t", None))

    assert add.await_args is not None
    kwargs = add.await_args.kwargs
    assert kwargs["previous_episode_uuids"] == []
    assert kwargs["entity_types"] is ENTITY_TYPES
    assert kwargs["excluded_entity_types"] == ["Entity"]
    assert kwargs["custom_extraction_instructions"] == extraction_instructions("Focus on lab equipment.", known)


@pytest.mark.asyncio
async def test_episodes_of_two_graphs_extracted_at_once_stay_apart(store: GraphitiStore) -> None:
    """Each episode lands in its own graph and charges its own conversation while another graph extracts alongside it."""
    both_extracting = asyncio.Event()
    charged: dict[str, set[str]] = {"alpha": set(), "beta": set()}

    async def extract(self: MindRoomLLMClient, messages: list[Message], response_model: type[BaseModel] | None) -> dict:
        del self
        episode = "alpha" if "alpha-only" in "\n".join(message.content for message in messages) else "beta"
        target = current_usage_target()
        assert target is not None
        charged[episode].add(target.session_id)
        if all(charged.values()):
            both_extracting.set()
        await both_extracting.wait()
        return {name: [] for name in response_model.model_fields} if response_model is not None else {}

    episodes = {
        scope: EpisodeInput("conversation", name, f"alice: {name}-only news", "conversation: room !r", JAN, ())
        for scope, name in ((ALPHA, "alpha"), (BETA, "beta"))
    }
    with patch.object(MindRoomLLMClient, "_extract", extract):
        await asyncio.wait_for(
            asyncio.gather(
                store.add_episode(ALPHA, episodes[ALPHA], UsageTarget("mind", "!r:$alpha", None)),
                store.add_episode(BETA, episodes[BETA], UsageTarget("mind", "!r:$beta", None)),
            ),
            timeout=30,
        )

    async def episode_names(scope: GraphScope) -> list[str]:
        graphiti = await store._graphiti_client()
        records, _, _ = await graphiti.driver.clone(database=scope.graph_name).execute_query(
            "MATCH (e:Episodic) RETURN e.name AS name",
        )
        return sorted(record["name"] for record in records)

    assert await store._run(episode_names(ALPHA)) == ["alpha"]
    assert await store._run(episode_names(BETA)) == ["beta"]
    assert charged == {"alpha": {"!r:$alpha"}, "beta": {"!r:$beta"}}
