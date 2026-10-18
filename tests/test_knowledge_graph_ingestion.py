"""The ingestion worker turns new finished turns into episodes once, backs off on failure, and stays within its caps."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from agno.db.base import SessionType
from agno.models.message import Message
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, resolve_runtime_paths
from mindroom.knowledge_graph import ingestion
from mindroom.knowledge_graph.ingestion import KnowledgeGraphIngestionWorker, wake_knowledge_graph_ingestion
from mindroom.knowledge_graph.scope import graph_scope
from mindroom.knowledge_graph.store import (
    UnusableExtractionError,
    knowledge_graph_failure,
    record_knowledge_graph_failure,
)
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.session_storage.agent_storage import create_session_storage
from tests.conftest import seed_session

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.history.session_runs import RedactedEventIds
    from mindroom.knowledge_graph.episodes import RenderedEpisode
    from mindroom.knowledge_graph.scope import GraphScope
    from mindroom.knowledge_graph.store import EpisodeInput, GraphFact, _GraphEntity
    from mindroom.knowledge_graph.usage import UsageTarget

DAY = 86_400
T0 = 1_760_000_000


@dataclass
class _FakeStore:
    """Records episodes; sessions in ``failing``, kinds in ``failing_kinds``, and bodies containing ``failing_text`` fail."""

    episodes: list[tuple[str, EpisodeInput, UsageTarget]] = field(default_factory=list)
    failing: set[str] = field(default_factory=set)
    failing_kinds: set[str] = field(default_factory=set)
    failing_text: str | None = None
    # An error every call raises, such as an outage or a setup error, while set.
    error: Exception | None = None
    added: asyncio.Event = field(default_factory=asyncio.Event)

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        if self.error is not None:
            raise self.error
        if (
            usage.session_id in self.failing
            or episode.kind in self.failing_kinds
            or (self.failing_text is not None and self.failing_text in episode.body)
        ):
            msg = "structured output did not match"
            raise UnusableExtractionError(msg)
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
        """Return the conversation of each ingested conversation episode, in order."""
        return [usage.session_id for _, episode, usage in self.episodes if episode.kind == "conversation"]


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


def _run(
    run_id: str,
    session_id: str,
    created_at: int,
    status: RunStatus = RunStatus.completed,
    agent: str = "mind",
    *,
    matrix: bool = True,
) -> RunOutput:
    return RunOutput(
        run_id=run_id,
        session_id=session_id,
        agent_id=agent,
        created_at=created_at,
        status=status,
        tools=[
            ToolExecution(
                tool_call_id=f"call-{run_id}",
                tool_name="get_issue",
                tool_args={"n": 1},
                result=f"Issue for {run_id}",
            ),
        ],
        metadata={
            # A turn through the OpenAI-compatible API names no room, so it is not a Matrix turn.
            **({"room_id": "!room:x"} if matrix else {}),
            "thread_id": session_id.rsplit(":", 1)[-1],
            "requester_id": "@alice:x",
            MATRIX_EVENT_ID_METADATA_KEY: f"$event-{run_id}",
        },
        messages=[
            Message(role="user", content=f"question {run_id}", created_at=created_at),
            Message(role="assistant", content=f"answer {run_id}", created_at=created_at),
        ],
    )


def _seed(
    config: Config,
    paths: RuntimePaths,
    session_id: str,
    run_ids: list[str],
    *,
    at: int = T0,
    paused: frozenset[str] = frozenset(),
    updated_at: int | None = None,
    agent: str = "mind",
    spacing: int = 1,
    matrix: bool = True,
) -> None:
    """Store a conversation whose runs start at ``at``, ``spacing`` seconds apart, last updated after its last run."""
    storage = create_session_storage(agent, config, paths, execution_identity=None)
    try:
        runs = [
            _run(
                run_id,
                session_id,
                at + index * spacing,
                RunStatus.paused if run_id in paused else RunStatus.completed,
                agent,
                matrix=matrix,
            )
            for index, run_id in enumerate(run_ids)
        ]
        seed_session(storage, AgentSession(session_id=session_id, agent_id=agent, runs=runs))
        with storage.db_engine.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE {storage.session_table_name} SET updated_at = ? WHERE session_id = ?",  # noqa: S608
                (updated_at if updated_at is not None else at + (len(run_ids) - 1) * spacing + 1, session_id),
            )
    finally:
        storage.close()


def _edit_runs(config: Config, paths: RuntimePaths, session_id: str, edit: Callable[[list[RunOutput]], None]) -> None:
    """Change a seeded conversation's stored runs in place."""
    storage = create_session_storage("mind", config, paths, execution_identity=None)
    try:
        session = storage.get_session(session_id, SessionType.AGENT)
        assert isinstance(session, AgentSession)
        edit(session.runs)
        for run in session.runs:
            storage.upsert_run(run=run, session_id=session_id, user_id=None)
    finally:
        storage.close()


async def _no_redactions(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
    del agent_name, room_id, event_ids
    return frozenset()


def _worker(
    config: Config,
    paths: RuntimePaths,
    store: _FakeStore,
    redacted_event_ids: RedactedEventIds = _no_redactions,
) -> KnowledgeGraphIngestionWorker:
    return KnowledgeGraphIngestionWorker(
        runtime_paths=paths,
        config_provider=lambda: config,
        store=store,
        redacted_event_ids=redacted_event_ids,
    )


def _answers(store: _FakeStore) -> list[str]:
    return [line for _, episode, _ in store.episodes for line in episode.body.splitlines() if line.startswith("Mind:")]


@pytest.mark.asyncio
async def test_cycle_ingests_new_runs_and_advances_its_marker(tmp_path: Path) -> None:
    """Each finished turn is ingested once; a later cycle sends only the turns added since."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"])
    worker = _worker(config, paths, store)

    await worker._run_cycle(config, now=T0 + 10)
    _seed(config, paths, "!room:x:$t1", ["r1", "r2", "r3"])
    await worker._run_cycle(config, now=T0 + 20)

    assert _answers(store) == ["Mind: answer r1", "Mind: answer r2", "Mind: answer r3"]
    assert {graph for graph, _, _ in store.episodes} == {graph_scope("mind", config, paths, None).graph_name}
    assert store.episodes[0][2].session_id == "!room:x:$t1"
    # A shared agent's extraction cost belongs to the person whose conversation it came from.
    assert {usage.requester_id for _, _, usage in store.episodes} == {"@alice:x"}


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
    # The unauthenticated health endpoint gets the failure's kind; error text can quote conversation content.
    detail = knowledge_graph_failure()
    assert detail is not None
    assert "UnusableExtractionError" in detail
    assert "structured output did not match" not in detail
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
    """A pass ingests at most the configured number of conversations from a graph; the rest wait for the next."""
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
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert store.sessions() == ["!room:x:$t1"]


@pytest.mark.asyncio
async def test_a_failing_episode_never_re_adds_the_one_beside_it(tmp_path: Path) -> None:
    """A tool-results episode that keeps failing is retried alone and dropped after five tries."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing_kinds={"tool_results"})
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)

    for attempt in range(7):
        await worker._run_cycle(config, now=T0 + 10 + attempt * 4000)

    assert [episode.kind for _, episode, _ in store.episodes] == ["conversation"]
    store.failing_kinds.clear()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"])
    await worker._run_cycle(config, now=T0 + 40_000)
    assert [episode.kind for _, episode, _ in store.episodes] == ["conversation", "conversation", "tool_results"]


@pytest.mark.asyncio
async def test_zero_backfill_ingests_conversations_after_the_graph_started(tmp_path: Path) -> None:
    """With no backfill, conversations from after the graph was turned on are still ingested."""
    config, paths, store = _config(backfill_days=0), _paths(tmp_path), _FakeStore()
    worker = _worker(config, paths, store)
    await worker._run_cycle(config, now=T0)
    _seed(config, paths, "!room:x:$t1", ["r1"], at=T0 + 100)

    await worker._run_cycle(config, now=T0 + 200)

    assert store.sessions() == ["!room:x:$t1"]


@pytest.mark.asyncio
async def test_a_paused_turn_holds_back_later_turns_until_it_finishes(tmp_path: Path) -> None:
    """A turn waiting for approval is ingested when it finishes, even after later turns."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], paused=frozenset({"r1"}))
    worker = _worker(config, paths, store)

    await worker._run_cycle(config, now=T0 + 10)
    assert store.episodes == []
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], updated_at=T0 + 15)
    await worker._run_cycle(config, now=T0 + 20)

    assert _answers(store) == ["Mind: answer r1", "Mind: answer r2"]


@pytest.mark.asyncio
async def test_one_failing_agent_does_not_stop_the_others(tmp_path: Path) -> None:
    """An agent whose conversations cannot be read is reported, and other agents are still ingested."""
    config = _config()
    config.agents["pal"] = AgentConfig(display_name="Pal", knowledge_graph=True)
    paths, store = _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1"])
    real = ingestion.create_session_storage

    def storage(agent_name: str, *args: object, **kwargs: object) -> object:
        if agent_name == "pal":
            msg = "unreadable session store"
            raise OSError(msg)
        return real(agent_name, *args, **kwargs)

    worker = _worker(config, paths, store)
    with patch.object(ingestion, "create_session_storage", storage):
        await worker._run_cycle(config, now=T0 + 10)
        # A pass that only fails to list adds nothing, so the worker waits instead of retrying at once.
        assert await worker._run_cycle(config, now=T0 + 20) is False

    assert store.sessions() == ["!room:x:$t1"]
    assert "OSError" in (knowledge_graph_failure() or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ConnectionError("graph database unavailable"), id="outage"),
        # A ValueError from setup, such as an unsupported embedder, is not output the model got wrong.
        pytest.param(ValueError("Unsupported embedder provider"), id="setup-error"),
    ],
)
async def test_an_outage_or_setup_error_never_drops_episodes(tmp_path: Path, error: Exception) -> None:
    """While the database or provider is down or misconfigured for hours, episodes wait instead of being dropped."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(error=error)
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)

    for attempt in range(8):
        await worker._run_cycle(config, now=T0 + 10 + attempt * 4000)
    store.error = None
    await worker._run_cycle(config, now=T0 + 40_000)

    assert [episode.kind for _, episode, _ in store.episodes] == ["conversation", "tool_results"]


@pytest.mark.asyncio
async def test_a_success_resets_the_failure_count(tmp_path: Path) -> None:
    """Failures of one episode never count against the next."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing_kinds={"conversation"})
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)
    for attempt in range(4):
        await worker._run_cycle(config, now=T0 + 10 + attempt * 4000)
    store.failing_kinds = {"tool_results"}
    await worker._run_cycle(config, now=T0 + 20_000)
    store.failing_kinds = set()
    await worker._run_cycle(config, now=T0 + 40_000)

    assert [episode.kind for _, episode, _ in store.episodes] == ["conversation", "tool_results"]


@pytest.mark.asyncio
async def test_a_new_graph_skips_turns_older_than_the_backfill_window(tmp_path: Path) -> None:
    """A long conversation still active today contributes only its recent turns to a new graph."""
    config, paths, store = _config(backfill_days=1), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["old", "new"], at=T0 - 5 * DAY, spacing=5 * DAY - 60, updated_at=T0)

    await _worker(config, paths, store)._run_cycle(config, now=T0 + 10)

    assert _answers(store) == ["Mind: answer new"]


@pytest.mark.asyncio
async def test_a_later_pass_names_a_person_as_their_earlier_turns_did(tmp_path: Path) -> None:
    """A turn stored without a display name, such as a stopped one, names its sender as the conversation did before."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()

    def tagged(runs: list[RunOutput]) -> None:
        for run in runs:
            display_name = "Alice Example" if run.run_id == "r1" else None
            body = f"question {run.run_id}"
            run.messages[0].content = render_msg_tag(sender="@alice:x", display_name=display_name, body=body)

    _seed(config, paths, "!room:x:$t1", ["r1"])
    _edit_runs(config, paths, "!room:x:$t1", tagged)
    worker = _worker(config, paths, store)
    await worker._run_cycle(config, now=T0 + 10)
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"])
    _edit_runs(config, paths, "!room:x:$t1", tagged)
    await worker._run_cycle(config, now=T0 + 20)

    conversations = [episode for _, episode, _ in store.episodes if episode.kind == "conversation"]
    assert [episode.body.splitlines()[0] for episode in conversations] == [
        "Alice Example: question r1",
        "Alice Example: question r2",
    ]
    assert [entity.name for entity in conversations[-1].known_entities] == ["Mind", "Alice Example"]


@pytest.mark.asyncio
async def test_a_long_backlog_spreads_over_passes(tmp_path: Path) -> None:
    """One conversation adds a bounded number of episodes per pass and stays due until it is done."""
    config, paths, store = _config(max_episode_chars=1000, max_tool_result_chars=400), _paths(tmp_path), _FakeStore()
    run_ids = [f"r{index:02d}" for index in range(30)]
    _seed(config, paths, "!room:x:$t1", run_ids)

    def long_answers(runs: list[RunOutput]) -> None:
        for run in runs:
            run.messages[1].content = "a" * 600

    _edit_runs(config, paths, "!room:x:$t1", long_answers)
    worker = _worker(config, paths, store)

    await worker._run_cycle(config, now=T0 + 100)
    first = len(store.sessions())
    await worker._run_cycle(config, now=T0 + 200)

    assert 0 < first < 30
    assert len(store.sessions()) == 30


@pytest.mark.asyncio
async def test_a_turn_resuming_right_after_its_approval_expires_is_still_waited_for(tmp_path: Path) -> None:
    """An expired approval resumes its turn a little after the window, so the turns after it wait a while longer."""
    config, paths, store = _config(backfill_days=30), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], paused=frozenset({"r1"}))

    await _worker(config, paths, store)._run_cycle(config, now=T0 + 7 * DAY + 600)

    assert store.episodes == []


@pytest.mark.asyncio
async def test_an_episode_failing_for_days_is_dropped(tmp_path: Path) -> None:
    """A rejection that never clears, such as a request the provider always refuses, stops costing calls."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(error=ConnectionError("graph database unavailable"))
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)

    await worker._run_cycle(config, now=T0 + 10)
    await worker._run_cycle(config, now=T0 + 10 + 3 * DAY + 1)
    store.error = None
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"])
    await worker._run_cycle(config, now=T0 + 10 + 4 * DAY)

    assert _answers(store) == ["Mind: answer r2"]


@pytest.mark.asyncio
async def test_a_conversation_held_back_by_an_approval_is_ingested_once_it_expires(tmp_path: Path) -> None:
    """Turns after an abandoned approval are ingested once its window passes, without another message."""
    config, paths, store = _config(backfill_days=30), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], paused=frozenset({"r1"}))
    worker = _worker(config, paths, store)

    await worker._run_cycle(config, now=T0 + 10)
    assert store.episodes == []
    await worker._run_cycle(config, now=T0 + 8 * DAY)

    assert _answers(store) == ["Mind: answer r2"]


@pytest.mark.asyncio
async def test_two_failing_pieces_of_a_split_run_still_let_it_finish(tmp_path: Path) -> None:
    """Dropped pieces stay done, so earlier pieces are not extracted again and the run completes."""
    config, paths = _config(max_episode_chars=1000, max_tool_result_chars=400), _paths(tmp_path)
    _seed(config, paths, "!room:x:$t1", ["r1"])

    def eight_results(runs: list[RunOutput]) -> None:
        runs[0].tools = [
            ToolExecution(tool_call_id=f"c{index}", tool_name=f"fetch_{index}", tool_args={}, result="x" * 380)
            for index in range(8)
        ]

    _edit_runs(config, paths, "!room:x:$t1", eight_results)
    store = _FakeStore()
    first_pieces: list[str] = []
    real_add = store.add_episode

    async def two_pieces_fail(scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        if episode.kind == "tool_results":
            first_call = episode.body.split("(", 1)[0]
            if first_call == "call fetch_0":
                first_pieces.append(first_call)
            if first_call in {"call fetch_2", "call fetch_4"}:
                msg = "unusable output"
                raise UnusableExtractionError(msg)
        await real_add(scope, episode, usage)

    worker = _worker(config, paths, store)
    with patch.object(store, "add_episode", two_pieces_fail):
        for attempt in range(14):
            await worker._run_cycle(config, now=T0 + 10 + attempt * 4000)

    assert first_pieces == ["call fetch_0"]
    assert any("fetch_7" in episode.body for _, episode, _ in store.episodes)


@pytest.mark.asyncio
async def test_a_turn_waiting_on_a_recent_approval_still_holds_back(tmp_path: Path) -> None:
    """An approval asked for days into a turn expires counting from when it was asked, not from the turn's start."""
    config, paths, store = _config(backfill_days=30), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], paused=frozenset({"r1"}))

    def asked_on_day_six(runs: list[RunOutput]) -> None:
        runs[0].messages[-1].created_at = T0 + 6 * DAY

    _edit_runs(config, paths, "!room:x:$t1", asked_on_day_six)

    await _worker(config, paths, store)._run_cycle(config, now=T0 + 8 * DAY)

    assert store.episodes == []


@pytest.mark.asyncio
async def test_an_unreadable_conversation_does_not_block_the_agents_others(tmp_path: Path) -> None:
    """A conversation whose turns cannot be read is reported, and the agent's other conversations are still added."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$bad", ["b1"], at=T0 - 5)
    _seed(config, paths, "!room:x:$good", ["g1"])
    real = ingestion.session_runs

    def runs(storage: object, session_id: str) -> list[RunOutput]:
        if session_id == "!room:x:$bad":
            msg = "corrupt session row"
            raise ValueError(msg)
        return real(storage, session_id)

    with patch.object(ingestion, "session_runs", runs):
        await _worker(config, paths, store)._run_cycle(config, now=T0 + 10)

    assert store.sessions() == ["!room:x:$good"]
    assert knowledge_graph_failure() is not None


@pytest.mark.asyncio
async def test_a_conversation_whose_redactions_cannot_be_checked_does_not_block_the_agents_others(
    tmp_path: Path,
) -> None:
    """A failing redaction lookup is reported for its conversation, and the agent's later conversations are added."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["b1"], at=T0 - 5)
    _seed(config, paths, "!room:x:$t2", ["g1"])

    async def redacted(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del agent_name, room_id
        if "$event-b1" in event_ids:
            msg = "event journal unavailable"
            raise RuntimeError(msg)
        return frozenset()

    await _worker(config, paths, store, redacted)._run_cycle(config, now=T0 + 10)

    assert store.sessions() == ["!room:x:$t2"]
    assert knowledge_graph_failure() is not None
    # Once the lookup works again, the conversation is added after its retry wait.
    await _worker(config, paths, store)._run_cycle(config, now=T0 + 100)
    assert store.sessions() == ["!room:x:$t2", "!room:x:$t1"]


@pytest.mark.asyncio
async def test_conversations_without_matrix_turns_take_no_place_in_a_pass(tmp_path: Path) -> None:
    """A conversation with nothing to ingest, such as one through the OpenAI-compatible API, leaves the pass to others."""
    config, paths, store = _config(max_sessions_per_cycle=1), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "api-1", ["x1"], at=T0 - 5, matrix=False)
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)
    await worker._run_cycle(config, now=T0 + 10)
    assert store.sessions() == ["!room:x:$t1"]

    # A pass that only sees such a conversation still records it, so it is not read again until it changes.
    _seed(config, paths, "api-2", ["x2"], at=T0 + 15, matrix=False)
    await worker._run_cycle(config, now=T0 + 20)
    with patch.object(ingestion, "session_runs", wraps=ingestion.session_runs) as read:
        await worker._run_cycle(config, now=T0 + 30)
    read.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["redaction check", "unreadable"])
async def test_a_failed_conversation_keeps_health_failing_until_it_succeeds(tmp_path: Path, failure: str) -> None:
    """A pass that adds others but never reaches a failed conversation, waiting for its retry, keeps health failing."""
    config, paths, store = _config(max_sessions_per_cycle=1), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["a1"], at=T0 - 5)
    _seed(config, paths, "!room:x:$t2", ["b1"])
    failing = True
    real = ingestion.session_runs

    async def redacted(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del agent_name, room_id
        if failure == "redaction check" and failing and "$event-a1" in event_ids:
            msg = "event journal unavailable"
            raise RuntimeError(msg)
        return frozenset()

    def runs(storage: object, session_id: str) -> list[RunOutput]:
        if failure == "unreadable" and failing and session_id == "!room:x:$t1":
            msg = "corrupt session row"
            raise ValueError(msg)
        return real(storage, session_id)

    worker = _worker(config, paths, store, redacted)
    with patch.object(ingestion, "session_runs", runs):
        await worker._run_cycle(config, now=T0 + 10)
        # A new turn sorts the failed conversation last, so the next passes spend their budget before reaching it.
        _seed(config, paths, "!room:x:$t1", ["a1", "a2"], at=T0 - 5, updated_at=T0 + 25)
        _seed(config, paths, "!room:x:$t3", ["c1"], at=T0 + 12)
        for now in (T0 + 30, T0 + 31):
            await worker._run_cycle(config, now=now)
        assert store.sessions() == ["!room:x:$t2", "!room:x:$t3"]
        assert knowledge_graph_failure() is not None

        failing = False
        # Fixed, it still waits out its retry before it is tried again.
        await worker._run_cycle(config, now=T0 + 40)
        assert store.sessions() == ["!room:x:$t2", "!room:x:$t3"]
        await worker._run_cycle(config, now=T0 + 100)
    assert store.sessions() == ["!room:x:$t2", "!room:x:$t3", "!room:x:$t1"]
    assert knowledge_graph_failure() is None


@pytest.mark.asyncio
async def test_a_busy_agent_does_not_hold_up_the_others(tmp_path: Path) -> None:
    """Each graph has its own budget in a pass, so one with a long backlog cannot keep another waiting."""
    config = _config(max_sessions_per_cycle=1)
    config.agents["pal"] = AgentConfig(display_name="Pal", knowledge_graph=True)
    paths, store = _paths(tmp_path), _FakeStore()
    for thread in ("$m1", "$m2", "$m3"):
        _seed(config, paths, f"!room:x:{thread}", [f"r{thread}"])
    _seed(config, paths, "!room:x:$p1", ["rp1"], agent="pal")

    await _worker(config, paths, store)._run_cycle(config, now=T0 + 10)

    assert "!room:x:$p1" in store.sessions()
    assert len(set(store.sessions())) == 2


@dataclass
class _OverlapStore(_FakeStore):
    """Holds each graph's episode until another graph has started one, so it finishes only when graphs overlap."""

    started: set[str] = field(default_factory=set)
    overlapping: asyncio.Event = field(default_factory=asyncio.Event)

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        self.started.add(scope.graph_name)
        if len(self.started) > 1:
            self.overlapping.set()
        await self.overlapping.wait()
        await super().add_episode(scope, episode, usage)


@pytest.mark.asyncio
async def test_graphs_are_ingested_at_the_same_time(tmp_path: Path) -> None:
    """One graph's extraction does not wait for another's, so a long backlog in one leaves the others on time."""
    config = _config()
    config.agents["pal"] = AgentConfig(display_name="Pal", knowledge_graph=True)
    paths, store = _paths(tmp_path), _OverlapStore()
    _seed(config, paths, "!room:x:$m1", ["m1"])
    _seed(config, paths, "!room:x:$p1", ["p1"], agent="pal")

    await asyncio.wait_for(_worker(config, paths, store)._run_cycle(config, now=T0 + 10), timeout=10)

    assert sorted(store.sessions()) == ["!room:x:$m1", "!room:x:$p1"]


@dataclass
class _InFlightStore(_FakeStore):
    """Holds the first episode until another graph starts one, and records whether one did."""

    first_graph: str | None = None
    overlapped: asyncio.Event = field(default_factory=asyncio.Event)

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        if self.first_graph is None:
            self.first_graph = scope.graph_name
            # Long enough for the other graph to reach its first episode, were nothing holding it back.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.overlapped.wait(), timeout=0.5)
        elif scope.graph_name != self.first_graph and len(self.episodes) == 0:
            self.overlapped.set()
        await super().add_episode(scope, episode, usage)


@pytest.mark.asyncio
async def test_no_more_graphs_than_the_limit_are_ingested_at_once(tmp_path: Path) -> None:
    """Graphs beyond the limit wait for a free place, so many private instances cannot flood the model provider."""
    config = _config()
    config.agents["pal"] = AgentConfig(display_name="Pal", knowledge_graph=True)
    paths, store = _paths(tmp_path), _InFlightStore()
    _seed(config, paths, "!room:x:$m1", ["m1"])
    _seed(config, paths, "!room:x:$p1", ["p1"], agent="pal")

    with patch.object(ingestion, "_CONCURRENT_GRAPHS", 1):
        await _worker(config, paths, store)._run_cycle(config, now=T0 + 10)

    assert sorted(store.sessions()) == ["!room:x:$m1", "!room:x:$p1"]
    assert not store.overlapped.is_set()


@pytest.mark.asyncio
async def test_a_pass_that_ingested_something_starts_the_next_at_once(tmp_path: Path) -> None:
    """A backlog larger than one pass is worked through without waiting the interval between passes."""
    config = _config(max_sessions_per_cycle=1, interval_seconds=3600)
    paths, store = _paths(tmp_path), _FakeStore()
    for thread in ("$t1", "$t2"):
        _seed(config, paths, f"!room:x:{thread}", [f"r{thread}"], at=int(time.time()) - 10)
    task = asyncio.create_task(_worker(config, paths, store).run())

    async def both_ingested() -> None:
        while len(set(store.sessions())) < 2:
            store.added.clear()
            await store.added.wait()

    try:
        await asyncio.wait_for(both_ingested(), timeout=10)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_pass_that_added_nothing_waits_for_the_interval(tmp_path: Path) -> None:
    """The worker does not spin while there is nothing to add; only a wake or the interval starts the next pass."""
    config, paths = _config(interval_seconds=3600), _paths(tmp_path)
    worker = _worker(config, paths, _FakeStore())
    passes = 0
    first_pass = asyncio.Event()

    async def empty_pass(config: Config, *, now: float) -> bool:
        del config, now
        nonlocal passes
        passes += 1
        first_pass.set()
        await asyncio.sleep(0)
        return False

    with patch.object(worker, "_run_cycle", empty_pass):
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(first_pass.wait(), timeout=10)
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert passes == 1


@pytest.mark.asyncio
async def test_a_pass_whose_every_attempt_failed_added_nothing(tmp_path: Path) -> None:
    """During a provider outage the worker waits for the interval instead of trying every due conversation at once."""
    config, paths = _config(max_sessions_per_cycle=1), _paths(tmp_path)
    store = _FakeStore(error=ConnectionError("provider outage"))
    for thread in ("$t1", "$t2"):
        _seed(config, paths, f"!room:x:{thread}", [f"r{thread}"])

    assert await _worker(config, paths, store)._run_cycle(config, now=T0 + 10) is False
    assert knowledge_graph_failure() is not None


@pytest.mark.asyncio
async def test_a_redacted_message_is_never_added(tmp_path: Path) -> None:
    """A turn whose message was redacted before ingestion reached it stays out of the graph with every later turn."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2", "r3"])
    asked: list[tuple[str, str, tuple[str, ...]]] = []

    async def redacted(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        asked.append((agent_name, room_id, event_ids))
        return frozenset({"$event-r2"})

    await _worker(config, paths, store, redacted)._run_cycle(config, now=T0 + 10)

    assert _answers(store) == ["Mind: answer r1"]
    assert asked == [("mind", "!room:x", ("$event-r1", "$event-r2", "$event-r3"))]


@pytest.mark.asyncio
async def test_a_conversation_waits_while_its_agent_cannot_say_what_was_redacted(tmp_path: Path) -> None:
    """Before the agent's bot runs, nothing is added, and the conversation is added once it can tell."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1"])

    async def not_running(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del agent_name, room_id, event_ids
        return None

    await _worker(config, paths, store, not_running)._run_cycle(config, now=T0 + 10)
    assert store.episodes == []
    await _worker(config, paths, store)._run_cycle(config, now=T0 + 20)
    assert _answers(store) == ["Mind: answer r1"]


@pytest.mark.asyncio
async def test_health_keeps_failing_while_a_conversation_waits_to_retry(tmp_path: Path) -> None:
    """Another conversation added cleanly does not clear the failure of one that is still waiting for its retry."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing={"!room:x:$bad"})
    _seed(config, paths, "!room:x:$bad", ["b1"])
    worker = _worker(config, paths, store)
    await worker._run_cycle(config, now=T0 + 10)

    _seed(config, paths, "!room:x:$good", ["g1"], at=T0 + 20)
    await worker._run_cycle(config, now=T0 + 40)

    assert store.sessions() == ["!room:x:$good"]
    assert knowledge_graph_failure() is not None


@pytest.mark.asyncio
async def test_a_failing_conversation_left_without_matrix_turns_stops_keeping_health_failing(tmp_path: Path) -> None:
    """A conversation whose failing turn is gone has nothing left to retry, so the next clean pass clears health."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing={"!room:x:$t1"})
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)
    await worker._run_cycle(config, now=T0 + 10)
    assert knowledge_graph_failure() is not None

    _seed(config, paths, "!room:x:$t1", ["r1"], updated_at=T0 + 4000, matrix=False)
    await worker._run_cycle(config, now=T0 + 5000)

    assert store.episodes == []
    assert knowledge_graph_failure() is None


@pytest.mark.asyncio
async def test_a_conversation_first_recorded_empty_still_starts_after_the_backfill_window(tmp_path: Path) -> None:
    """Turns from before a new graph's backfill window stay out, even when their conversation was first seen empty."""
    config, paths, store = _config(backfill_days=0), _paths(tmp_path), _FakeStore()
    worker = _worker(config, paths, store)
    await worker._run_cycle(config, now=T0 + 10)
    # A turn that has not finished, such as one still running when the graph started, is not a Matrix turn yet.
    _seed(config, paths, "!room:x:$t1", ["old"], at=T0 + 5, updated_at=T0 + 20, matrix=False)
    await worker._run_cycle(config, now=T0 + 30)

    _seed(config, paths, "!room:x:$t1", ["old", "new"], at=T0 + 5, spacing=35)
    await worker._run_cycle(config, now=T0 + 60)

    assert _answers(store) == ["Mind: answer new"]


@pytest.mark.asyncio
async def test_a_graph_no_longer_maintained_stops_keeping_health_failing(tmp_path: Path) -> None:
    """After an embedder change, the old graph's failing episode no longer holds the failure once the new one is fine."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing={"!room:x:$t1"})
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)
    await worker._run_cycle(config, now=T0 + 10)
    assert knowledge_graph_failure() is not None

    store.failing.clear()
    changed = config.model_copy(deep=True)
    changed.memory.embedder.config.dimensions = 256
    await worker._run_cycle(changed, now=T0 + 20)

    assert store.sessions() == ["!room:x:$t1"]
    assert knowledge_graph_failure() is None


@pytest.mark.asyncio
async def test_a_conversation_whose_agent_is_not_running_counts_for_nothing(tmp_path: Path) -> None:
    """It does not clear an earlier failure, while an agent that is running is still ingested."""
    config = _config(max_sessions_per_cycle=1)
    config.agents["pal"] = AgentConfig(display_name="Pal", knowledge_graph=True)
    paths, store = _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$mind", ["m1"])
    record_knowledge_graph_failure("Knowledge graph ingestion failed: UnusableExtractionError")

    async def only_pal_runs(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del room_id, event_ids
        return frozenset() if agent_name == "pal" else None

    # Nor does it start the next pass at once, which would spin while the agent stays down.
    assert await _worker(config, paths, store, only_pal_runs)._run_cycle(config, now=T0 + 10) is False
    assert knowledge_graph_failure() is not None

    _seed(config, paths, "!room:x:$pal", ["p1"], agent="pal")
    assert await _worker(config, paths, store, only_pal_runs)._run_cycle(config, now=T0 + 20) is True
    assert store.sessions() == ["!room:x:$pal"]


@pytest.mark.asyncio
async def test_a_failing_tool_result_is_dropped_while_the_conversation_keeps_succeeding(tmp_path: Path) -> None:
    """New turns that extract fine do not reset the count of the tool results that keep failing behind them."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing_text="Issue for r0")
    worker = _worker(config, paths, store)

    for day in range(7):
        _seed(config, paths, "!room:x:$t1", [f"r{index}" for index in range(day + 1)], spacing=7200)
        await worker._run_cycle(config, now=T0 + day * 7200 + 100)

    assert [episode.kind for _, episode, _ in store.episodes].count("tool_results") >= 1


@pytest.mark.asyncio
async def test_episodes_are_rendered_off_the_event_loop(tmp_path: Path) -> None:
    """Rendering and redacting a long backlog must not stall every agent's replies."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1"])
    rendered_on: list[threading.Thread] = []
    real = ingestion.render_episodes

    def render(*args: object, **kwargs: object) -> list[RenderedEpisode]:
        rendered_on.append(threading.current_thread())
        return real(*args, **kwargs)

    with patch.object(ingestion, "render_episodes", render):
        await _worker(config, paths, store)._run_cycle(config, now=T0 + 10)

    assert rendered_on
    assert all(thread is not threading.main_thread() for thread in rendered_on)
