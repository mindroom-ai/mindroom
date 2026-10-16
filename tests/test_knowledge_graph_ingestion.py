"""The ingestion worker turns new finished turns into episodes once, backs off on failure, and stays within its caps."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge_graph.ingestion import KnowledgeGraphIngestionWorker, wake_knowledge_graph_ingestion
from mindroom.knowledge_graph.scope import graph_scope
from mindroom.knowledge_graph.store import knowledge_graph_failure, record_knowledge_graph_failure
from mindroom.session_storage.agent_storage import create_session_storage
from tests.conftest import seed_session

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.knowledge_graph.scope import GraphScope
    from mindroom.knowledge_graph.store import EpisodeInput, GraphFact, _GraphEntity
    from mindroom.knowledge_graph.usage import UsageTarget

DAY = 86_400
T0 = 1_760_000_000


@dataclass
class _FakeStore:
    """Records episodes; sessions listed in ``failing`` raise as a failing extraction would."""

    episodes: list[tuple[str, EpisodeInput, UsageTarget]] = field(default_factory=list)
    failing: set[str] = field(default_factory=set)
    added: asyncio.Event = field(default_factory=asyncio.Event)

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        if usage.session_id in self.failing:
            msg = "structured output did not match"
            raise ValueError(msg)
        self.episodes.append((scope.graph_name, episode, usage))
        self.added.set()

    async def search_facts(self, scope: GraphScope, query: str, limit: int) -> list[GraphFact]:
        del scope, query, limit
        return []

    async def entity(self, scope: GraphScope, name: str) -> _GraphEntity | None:
        del scope, name
        return None

    async def close(self) -> None:
        return None

    def sessions(self) -> list[str]:
        return [usage.session_id for _, _, usage in self.episodes]


@pytest.fixture(autouse=True)
def _clear_health() -> Iterator[None]:
    record_knowledge_graph_failure(None)
    yield
    record_knowledge_graph_failure(None)


def _config(**ingest: object) -> Config:
    return Config(
        agents={"mind": AgentConfig(display_name="Mind", knowledge_graph=True)},
        models={"luna": ModelConfig(provider="openai", id="gpt-6-luna")},
        knowledge_graph={"model": "luna", "ingest": {"idle_seconds": 1, **ingest}},
    )


def _paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)


def _run(run_id: str, session_id: str, created_at: int) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        session_id=session_id,
        agent_id="mind",
        created_at=created_at,
        status=RunStatus.completed,
        metadata={"room_id": "!room:x", "thread_id": session_id.rsplit(":", 1)[-1], "requester_id": "@alice:x"},
        messages=[
            Message(role="user", content=f"question {run_id}"),
            Message(role="assistant", content=f"answer {run_id}"),
        ],
    )


def _seed(config: Config, paths: RuntimePaths, session_id: str, run_ids: list[str], *, at: int = T0) -> None:
    """Store a conversation whose runs start at ``at``, one second apart, last updated with its last run."""
    storage = create_session_storage("mind", config, paths, execution_identity=None)
    try:
        runs = [_run(run_id, session_id, at + index) for index, run_id in enumerate(run_ids)]
        seed_session(storage, AgentSession(session_id=session_id, agent_id="mind", runs=runs))
        with storage.db_engine.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE {storage.session_table_name} SET updated_at = ? WHERE session_id = ?",  # noqa: S608
                (at + len(run_ids), session_id),
            )
    finally:
        storage.close()


def _worker(config: Config, paths: RuntimePaths, store: _FakeStore) -> KnowledgeGraphIngestionWorker:
    return KnowledgeGraphIngestionWorker(runtime_paths=paths, config_provider=lambda: config, store=store)


def _answers(store: _FakeStore) -> list[str]:
    return [line for _, episode, _ in store.episodes for line in episode.body.splitlines() if line.startswith("agent")]


@pytest.mark.asyncio
async def test_cycle_ingests_new_runs_and_advances_its_marker(tmp_path: Path) -> None:
    """Each finished turn is ingested once; a later cycle sends only the turns added since."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"])
    worker = _worker(config, paths, store)

    await worker._run_cycle(config, now=T0 + 10)
    _seed(config, paths, "!room:x:$t1", ["r1", "r2", "r3"])
    await worker._run_cycle(config, now=T0 + 20)

    assert _answers(store) == ["agent mind: answer r1", "agent mind: answer r2", "agent mind: answer r3"]
    assert {graph for graph, _, _ in store.episodes} == {graph_scope("mind", config, paths, None).graph_name}
    assert store.episodes[0][2].session_id == "!room:x:$t1"


@pytest.mark.asyncio
async def test_marker_survives_a_restart(tmp_path: Path) -> None:
    """A new worker over the same storage does not ingest turns again."""
    config, paths = _config(), _paths(tmp_path)
    _seed(config, paths, "!room:x:$t1", ["r1"])
    await _worker(config, paths, _FakeStore())._run_cycle(config, now=T0 + 10)

    restarted = _FakeStore()
    await _worker(config, paths, restarted)._run_cycle(config, now=T0 + 20)

    assert restarted.episodes == []


@pytest.mark.asyncio
async def test_failing_session_backs_off_while_others_still_ingest(tmp_path: Path) -> None:
    """One session's failing extraction does not block the rest, and it retries after a cooldown."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing={"!room:x:$bad"})
    _seed(config, paths, "!room:x:$bad", ["b1"])
    _seed(config, paths, "!room:x:$good", ["g1"])
    worker = _worker(config, paths, store)
    now = T0 + 10

    await worker._run_cycle(config, now=now)

    assert store.sessions() == ["!room:x:$good"]
    assert knowledge_graph_failure() is not None
    store.failing.clear()
    await worker._run_cycle(config, now=now + 1)
    assert store.sessions() == ["!room:x:$good"]
    await worker._run_cycle(config, now=now + 61)
    assert store.sessions() == ["!room:x:$good", "!room:x:$bad"]
    assert knowledge_graph_failure() is None


@pytest.mark.asyncio
async def test_active_sessions_wait_for_idle_seconds(tmp_path: Path) -> None:
    """A conversation still going on is ingested once it has been quiet long enough."""
    config, paths, store = _config(idle_seconds=120), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)

    await worker._run_cycle(config, now=T0 + 60)
    assert store.episodes == []
    await worker._run_cycle(config, now=T0 + 122)
    assert store.sessions() == ["!room:x:$t1"]


@pytest.mark.asyncio
async def test_max_sessions_per_cycle_is_respected(tmp_path: Path) -> None:
    """A pass ingests at most the configured number of conversations; the rest wait for the next."""
    config, paths, store = _config(max_sessions_per_cycle=2), _paths(tmp_path), _FakeStore()
    for index in range(3):
        _seed(config, paths, f"!room:x:$t{index}", [f"r{index}"])
    worker = _worker(config, paths, store)

    await worker._run_cycle(config, now=T0 + 10)
    assert len(set(store.sessions())) == 2
    await worker._run_cycle(config, now=T0 + 20)
    assert len(set(store.sessions())) == 3


@pytest.mark.asyncio
async def test_new_graph_backfills_only_recent_sessions(tmp_path: Path) -> None:
    """Turning the graph on starts from recent conversations instead of the whole archive."""
    config, paths, store = _config(backfill_days=7), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$old", ["r1"])

    await _worker(config, paths, store)._run_cycle(config, now=T0 + 30 * DAY)

    assert store.episodes == []


@pytest.mark.asyncio
async def test_embedder_change_rebuilds_into_a_new_graph(tmp_path: Path) -> None:
    """Another embedder means another graph, which is filled from the stored conversations again."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1"])
    await _worker(config, paths, store)._run_cycle(config, now=T0 + 10)

    changed = config.model_copy(deep=True)
    changed.memory.embedder.config.dimensions = 256
    await _worker(changed, paths, store)._run_cycle(changed, now=T0 + 20)

    assert store.sessions() == ["!room:x:$t1", "!room:x:$t1"]
    assert len({graph for graph, _, _ in store.episodes}) == 2


@pytest.mark.asyncio
async def test_wake_runs_a_pass_before_the_interval(tmp_path: Path) -> None:
    """An exploration run's results are ingested without waiting a full interval."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    worker = _worker(config, paths, store)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.sleep(0)
        _seed(config, paths, "!room:x:$t1", ["r1"], at=int(time.time()) - 10)
        wake_knowledge_graph_ingestion(after_seconds=0)
        await asyncio.wait_for(store.added.wait(), timeout=10)
    finally:
        worker.stop()
        await task

    assert store.sessions() == ["!room:x:$t1"]
