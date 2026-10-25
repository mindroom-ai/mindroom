"""The ingestion worker turns new finished turns into episodes once, backs off on failure, and stays within its caps."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

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
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.usage.storage import quote_identifier
from tests.conftest import seed_session

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator
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
) -> RunRecord:
    return RunRecord(
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
    seed_session(storage, ConversationSession(session_id=session_id, kind="agent", owner_id=agent, runs=runs))
    with storage.transaction(write=True) as connection:
        connection.execute(
            f"UPDATE {quote_identifier(storage.names.sessions)} SET updated_at = ? WHERE session_id = ?",  # noqa: S608
            (updated_at if updated_at is not None else at + (len(run_ids) - 1) * spacing + 1, session_id),
        )


def _edit_runs(config: Config, paths: RuntimePaths, session_id: str, edit: Callable[[list[RunRecord]], None]) -> None:
    """Change a seeded conversation's stored runs in place."""
    storage = create_session_storage("mind", config, paths, execution_identity=None)
    session = storage.load_session(session_id, "agent")
    assert session is not None
    runs = copy.deepcopy(session.runs)
    edit(runs)
    storage.save_runs(session, runs)


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


async def _pass(worker: KnowledgeGraphIngestionWorker, config: Config, *, now: float) -> bool:
    """Run one pass of every maintained graph at once, as the worker's graphs run side by side."""
    scopes = await worker._maintained_graphs(config)
    progress = await asyncio.gather(*(worker._ingest_graph(config, scope, now=now) for scope in scopes))
    return any(progress)


def _answers(store: _FakeStore) -> list[str]:
    return [line for _, episode, _ in store.episodes for line in episode.body.splitlines() if line.startswith("Mind:")]


@pytest.mark.asyncio
async def test_cycle_ingests_new_runs_and_advances_its_marker(tmp_path: Path) -> None:
    """Each finished turn is ingested once; a later cycle sends only the turns added since."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"])
    worker = _worker(config, paths, store)

    await _pass(worker, config, now=T0 + 10)
    _seed(config, paths, "!room:x:$t1", ["r1", "r2", "r3"])
    await _pass(worker, config, now=T0 + 20)

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
    await _pass(_worker(config, paths, _FakeStore()), config, now=T0 + 10)

    restarted = _FakeStore()
    await _pass(_worker(config, paths, restarted), config, now=T0 + 20)

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

    await _pass(worker, config, now=now)

    assert store.sessions() == ["!room:x:$good"]
    # The unauthenticated health endpoint gets the failure's kind; error text can quote conversation content.
    detail = knowledge_graph_failure()
    assert detail is not None
    assert "UnusableExtractionError" in detail
    assert "structured output did not match" not in detail
    store.failing.clear()
    await _pass(worker, config, now=now + 1)
    assert store.sessions() == ["!room:x:$good"]
    await _pass(worker, config, now=now + 61)
    assert store.sessions() == ["!room:x:$good", "!room:x:$bad"]
    assert knowledge_graph_failure() is None


@pytest.mark.asyncio
async def test_active_sessions_wait_for_idle_seconds(tmp_path: Path) -> None:
    """A conversation still going on is ingested once it has been quiet long enough."""
    config, paths, store = _config(idle_seconds=120), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)

    await _pass(worker, config, now=T0 + 60)
    assert store.episodes == []
    await _pass(worker, config, now=T0 + 122)
    assert store.sessions() == ["!room:x:$t1"]


@pytest.mark.asyncio
async def test_max_sessions_per_cycle_is_respected(tmp_path: Path) -> None:
    """A pass ingests at most the configured number of conversations from a graph; the rest wait for the next."""
    config, paths, store = _config(max_sessions_per_cycle=2), _paths(tmp_path), _FakeStore()
    for index in range(3):
        _seed(config, paths, f"!room:x:$t{index}", [f"r{index}"])
    worker = _worker(config, paths, store)

    await _pass(worker, config, now=T0 + 10)
    assert len(set(store.sessions())) == 2
    await _pass(worker, config, now=T0 + 20)
    assert len(set(store.sessions())) == 3


@pytest.mark.asyncio
async def test_new_graph_backfills_only_recent_sessions(tmp_path: Path) -> None:
    """Turning the graph on starts from recent conversations instead of the whole archive."""
    config, paths, store = _config(backfill_days=7), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$old", ["r1"])

    await _pass(_worker(config, paths, store), config, now=T0 + 30 * DAY)

    assert store.episodes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda config: setattr(config.memory.embedder.config, "dimensions", 256), id="embedder"),
        pytest.param(lambda config: setattr(config.knowledge_graph, "generation", 1), id="generation"),
    ],
)
async def test_another_embedder_or_generation_rebuilds_into_a_new_graph(
    tmp_path: Path,
    change: Callable[[Config], object],
) -> None:
    """Either names another graph, which the running worker fills from the stored conversations again."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)
    await _pass(worker, config, now=T0 + 10)

    change(config)
    await _pass(worker, config, now=T0 + 20)

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
        await _pass(worker, config, now=T0 + 10 + attempt * 4000)

    assert [episode.kind for _, episode, _ in store.episodes] == ["conversation"]
    store.failing_kinds.clear()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"])
    await _pass(worker, config, now=T0 + 40_000)
    assert [episode.kind for _, episode, _ in store.episodes] == ["conversation", "conversation", "tool_results"]


@pytest.mark.asyncio
async def test_zero_backfill_ingests_conversations_after_the_graph_started(tmp_path: Path) -> None:
    """With no backfill, conversations from after the graph was turned on are still ingested."""
    config, paths, store = _config(backfill_days=0), _paths(tmp_path), _FakeStore()
    worker = _worker(config, paths, store)
    await _pass(worker, config, now=T0)
    _seed(config, paths, "!room:x:$t1", ["r1"], at=T0 + 100)

    await _pass(worker, config, now=T0 + 200)

    assert store.sessions() == ["!room:x:$t1"]


@pytest.mark.asyncio
async def test_a_paused_turn_holds_back_later_turns_until_it_finishes(tmp_path: Path) -> None:
    """A turn waiting for approval is ingested when it finishes, even after later turns."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], paused=frozenset({"r1"}))
    worker = _worker(config, paths, store)

    await _pass(worker, config, now=T0 + 10)
    assert store.episodes == []
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], updated_at=T0 + 15)
    await _pass(worker, config, now=T0 + 20)

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
        await _pass(worker, config, now=T0 + 10)
        # A pass that only fails to list adds nothing, so the worker waits instead of retrying at once.
        assert await _pass(worker, config, now=T0 + 20) is False

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
        await _pass(worker, config, now=T0 + 10 + attempt * 4000)
    store.error = None
    await _pass(worker, config, now=T0 + 40_000)

    assert [episode.kind for _, episode, _ in store.episodes] == ["conversation", "tool_results"]


@pytest.mark.asyncio
async def test_a_success_resets_the_failure_count(tmp_path: Path) -> None:
    """Failures of one episode never count against the next."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing_kinds={"conversation"})
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)
    for attempt in range(4):
        await _pass(worker, config, now=T0 + 10 + attempt * 4000)
    store.failing_kinds = {"tool_results"}
    await _pass(worker, config, now=T0 + 20_000)
    store.failing_kinds = set()
    await _pass(worker, config, now=T0 + 40_000)

    assert [episode.kind for _, episode, _ in store.episodes] == ["conversation", "tool_results"]


@pytest.mark.asyncio
async def test_a_new_graph_skips_turns_older_than_the_backfill_window(tmp_path: Path) -> None:
    """A long conversation still active today contributes only its recent turns to a new graph."""
    config, paths, store = _config(backfill_days=1), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["old", "new"], at=T0 - 5 * DAY, spacing=5 * DAY - 60, updated_at=T0)

    await _pass(_worker(config, paths, store), config, now=T0 + 10)

    assert _answers(store) == ["Mind: answer new"]


@pytest.mark.asyncio
async def test_a_later_pass_names_a_person_as_their_earlier_turns_did(tmp_path: Path) -> None:
    """A turn stored without a display name, such as a stopped one, names its sender as the conversation did before."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()

    def tagged(runs: list[RunRecord]) -> None:
        for run in runs:
            display_name = "Alice Example" if run.run_id == "r1" else None
            body = f"question {run.run_id}"
            run.messages[0].content = render_msg_tag(sender="@alice:x", display_name=display_name, body=body)

    _seed(config, paths, "!room:x:$t1", ["r1"])
    _edit_runs(config, paths, "!room:x:$t1", tagged)
    worker = _worker(config, paths, store)
    await _pass(worker, config, now=T0 + 10)
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"])
    _edit_runs(config, paths, "!room:x:$t1", tagged)
    await _pass(worker, config, now=T0 + 20)

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

    def long_answers(runs: list[RunRecord]) -> None:
        for run in runs:
            run.messages[1].content = "a" * 600

    _edit_runs(config, paths, "!room:x:$t1", long_answers)
    worker = _worker(config, paths, store)

    await _pass(worker, config, now=T0 + 100)
    first = len(store.sessions())
    await _pass(worker, config, now=T0 + 200)

    assert 0 < first < 30
    assert len(store.sessions()) == 30


@pytest.mark.asyncio
async def test_a_turn_resuming_right_after_its_approval_expires_is_still_waited_for(tmp_path: Path) -> None:
    """An expired approval resumes its turn a little after the window, so the turns after it wait a while longer."""
    config, paths, store = _config(backfill_days=30), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], paused=frozenset({"r1"}))

    await _pass(_worker(config, paths, store), config, now=T0 + 7 * DAY + 600)

    assert store.episodes == []


@pytest.mark.asyncio
async def test_an_episode_failing_for_days_is_dropped(tmp_path: Path) -> None:
    """A rejection that never clears, such as a request the provider always refuses, stops costing calls."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(error=ConnectionError("graph database unavailable"))
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)

    await _pass(worker, config, now=T0 + 10)
    await _pass(worker, config, now=T0 + 10 + 3 * DAY + 1)
    store.error = None
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"])
    await _pass(worker, config, now=T0 + 10 + 4 * DAY)

    assert _answers(store) == ["Mind: answer r2"]


@pytest.mark.asyncio
async def test_a_conversation_held_back_by_an_approval_is_ingested_once_it_expires(tmp_path: Path) -> None:
    """Turns after an abandoned approval are ingested once its window passes, without another message."""
    config, paths, store = _config(backfill_days=30), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], paused=frozenset({"r1"}))
    worker = _worker(config, paths, store)

    await _pass(worker, config, now=T0 + 10)
    assert store.episodes == []
    await _pass(worker, config, now=T0 + 8 * DAY)

    assert _answers(store) == ["Mind: answer r2"]


@pytest.mark.asyncio
async def test_two_failing_pieces_of_a_split_run_still_let_it_finish(tmp_path: Path) -> None:
    """Dropped pieces stay done, so earlier pieces are not extracted again and the run completes."""
    config, paths = _config(max_episode_chars=1000, max_tool_result_chars=400), _paths(tmp_path)
    _seed(config, paths, "!room:x:$t1", ["r1"])

    def eight_results(runs: list[RunRecord]) -> None:
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
            await _pass(worker, config, now=T0 + 10 + attempt * 4000)

    assert first_pieces == ["call fetch_0"]
    assert any("fetch_7" in episode.body for _, episode, _ in store.episodes)


@pytest.mark.asyncio
async def test_a_turn_waiting_on_a_recent_approval_still_holds_back(tmp_path: Path) -> None:
    """An approval asked for days into a turn expires counting from when it was asked, not from the turn's start."""
    config, paths, store = _config(backfill_days=30), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$t1", ["r1", "r2"], paused=frozenset({"r1"}))

    def asked_on_day_six(runs: list[RunRecord]) -> None:
        runs[0].messages[-1].created_at = T0 + 6 * DAY

    _edit_runs(config, paths, "!room:x:$t1", asked_on_day_six)

    await _pass(_worker(config, paths, store), config, now=T0 + 8 * DAY)

    assert store.episodes == []


@pytest.mark.asyncio
async def test_an_unreadable_conversation_does_not_block_the_agents_others(tmp_path: Path) -> None:
    """A conversation whose turns cannot be read is reported, and the agent's other conversations are still added."""
    config, paths, store = _config(), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "!room:x:$bad", ["b1"], at=T0 - 5)
    _seed(config, paths, "!room:x:$good", ["g1"])
    real = ingestion.session_runs

    def runs(storage: object, session_id: str) -> list[RunRecord]:
        if session_id == "!room:x:$bad":
            msg = "corrupt session row"
            raise ValueError(msg)
        return real(storage, session_id)

    with patch.object(ingestion, "session_runs", runs):
        await _pass(_worker(config, paths, store), config, now=T0 + 10)

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

    await _pass(_worker(config, paths, store, redacted), config, now=T0 + 10)

    assert store.sessions() == ["!room:x:$t2"]
    assert knowledge_graph_failure() is not None
    # Once the lookup works again, the conversation is added after its retry wait.
    await _pass(_worker(config, paths, store), config, now=T0 + 100)
    assert store.sessions() == ["!room:x:$t2", "!room:x:$t1"]


@pytest.mark.asyncio
async def test_conversations_without_matrix_turns_take_no_place_in_a_pass(tmp_path: Path) -> None:
    """A conversation with nothing to ingest, such as one through the OpenAI-compatible API, leaves the pass to others."""
    config, paths, store = _config(max_sessions_per_cycle=1), _paths(tmp_path), _FakeStore()
    _seed(config, paths, "api-1", ["x1"], at=T0 - 5, matrix=False)
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)
    await _pass(worker, config, now=T0 + 10)
    assert store.sessions() == ["!room:x:$t1"]

    # A pass that only sees such a conversation still records it, so it is not read again until it changes.
    _seed(config, paths, "api-2", ["x2"], at=T0 + 15, matrix=False)
    await _pass(worker, config, now=T0 + 20)
    with patch.object(ingestion, "session_runs", wraps=ingestion.session_runs) as read:
        await _pass(worker, config, now=T0 + 30)
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

    def runs(storage: object, session_id: str) -> list[RunRecord]:
        if failure == "unreadable" and failing and session_id == "!room:x:$t1":
            msg = "corrupt session row"
            raise ValueError(msg)
        return real(storage, session_id)

    worker = _worker(config, paths, store, redacted)
    with patch.object(ingestion, "session_runs", runs):
        await _pass(worker, config, now=T0 + 10)
        # A new turn sorts the failed conversation last, so the next passes spend their budget before reaching it.
        _seed(config, paths, "!room:x:$t1", ["a1", "a2"], at=T0 - 5, updated_at=T0 + 25)
        _seed(config, paths, "!room:x:$t3", ["c1"], at=T0 + 12)
        for now in (T0 + 30, T0 + 31):
            await _pass(worker, config, now=now)
        assert store.sessions() == ["!room:x:$t2", "!room:x:$t3"]
        assert knowledge_graph_failure() is not None

        failing = False
        # Fixed, it still waits out its retry before it is tried again.
        await _pass(worker, config, now=T0 + 40)
        assert store.sessions() == ["!room:x:$t2", "!room:x:$t3"]
        await _pass(worker, config, now=T0 + 100)
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

    await _pass(_worker(config, paths, store), config, now=T0 + 10)

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

    await asyncio.wait_for(_pass(_worker(config, paths, store), config, now=T0 + 10), timeout=10)

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
    at = int(time.time()) - 10
    _seed(config, paths, "!room:x:$m1", ["m1"], at=at)
    _seed(config, paths, "!room:x:$p1", ["p1"], at=at, agent="pal")

    async def both_ingested() -> None:
        while len(set(store.sessions())) < 2:
            store.added.clear()
            await store.added.wait()

    with patch.object(ingestion, "_CONCURRENT_GRAPHS", 1):
        task = asyncio.create_task(_worker(config, paths, store).run())
        try:
            await asyncio.wait_for(both_ingested(), timeout=10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

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


@dataclass
class _HeldStore(_FakeStore):
    """Holds one graph's episodes until released, as a long extraction would, and counts the ones held now."""

    held_graph: str = ""
    hold_all: bool = False
    release: asyncio.Event = field(default_factory=asyncio.Event)
    holding: asyncio.Event = field(default_factory=asyncio.Event)
    in_flight: int = 0

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        if self.hold_all or scope.graph_name == self.held_graph:
            self.in_flight += 1
            self.holding.set()
            try:
                await self.release.wait()
            finally:
                self.in_flight -= 1
        await super().add_episode(scope, episode, usage)


@pytest.mark.asyncio
async def test_a_graph_still_extracting_does_not_hold_up_another_graphs_next_pass(tmp_path: Path) -> None:
    """Each graph runs its passes on its own, so one long extraction cannot keep another graph's backlog waiting."""
    config = _config(max_sessions_per_cycle=1, interval_seconds=3600)
    config.agents["pal"] = AgentConfig(display_name="Pal", knowledge_graph=True)
    paths = _paths(tmp_path)
    store = _HeldStore(held_graph=graph_scope("mind", config, paths, None).graph_name)
    at = int(time.time()) - 10
    _seed(config, paths, "!room:x:$m1", ["m1"], at=at)
    for thread in ("$p1", "$p2", "$p3"):
        _seed(config, paths, f"!room:x:{thread}", [f"r{thread}"], at=at, agent="pal")
    task = asyncio.create_task(_worker(config, paths, store).run())

    async def pal_backlog_ingested() -> None:
        while len(set(store.sessions())) < 3:
            store.added.clear()
            await store.added.wait()

    try:
        await asyncio.wait_for(pal_backlog_ingested(), timeout=10)
        assert sorted(store.sessions()) == ["!room:x:$p1", "!room:x:$p2", "!room:x:$p3"]
    finally:
        store.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_wake_does_not_start_a_second_pass_loop_for_a_graph_still_filling(tmp_path: Path) -> None:
    """A graph adds one episode at a time even when a wake arrives while its pass is still running."""
    config = _config(max_sessions_per_cycle=1, interval_seconds=3600)
    paths = _paths(tmp_path)
    store = _HeldStore(held_graph=graph_scope("mind", config, paths, None).graph_name)
    at = int(time.time()) - 10
    for thread in ("$t1", "$t2"):
        _seed(config, paths, f"!room:x:{thread}", [f"r{thread}"], at=at)
    worker = _worker(config, paths, store)
    looks = 0
    looked = asyncio.Event()
    fills = 0
    real_looks, real_fill = worker._maintained_graphs, worker._fill

    async def counting_looks(config: Config) -> list[GraphScope]:
        nonlocal looks
        scopes = await real_looks(config)
        looks += 1
        looked.set()
        return scopes

    def counting_fill(graph_name: str) -> Coroutine[None, None, None]:
        nonlocal fills
        fills += 1
        return real_fill(graph_name)

    with patch.object(worker, "_maintained_graphs", counting_looks), patch.object(worker, "_fill", counting_fill):
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(store.holding.wait(), timeout=10)
            before = looks
            wake_knowledge_graph_ingestion(after_seconds=0)
            while looks == before:
                looked.clear()
                await asyncio.wait_for(looked.wait(), timeout=10)
            # The look the wake started returned, and the worker decides on new pass loops before it next waits.
            await asyncio.sleep(0)
            assert fills == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Stopping the worker stops the graph's pass too, instead of leaving it extracting.
    assert store.in_flight == 0


@pytest.mark.asyncio
async def test_a_wake_during_a_pass_that_adds_nothing_still_starts_another(tmp_path: Path) -> None:
    """Turns stored while their graph's pass had already listed its conversations are added without waiting the interval."""
    config = _config(interval_seconds=3600)
    paths, store = _paths(tmp_path), _FakeStore()
    worker = _worker(config, paths, store)
    graph_name = graph_scope("mind", config, paths, None).graph_name
    first_pass = asyncio.Event()
    release = asyncio.Event()
    passes = 0
    real = worker._ingest_graph

    async def first_pass_finds_nothing(config: Config, scope: GraphScope, *, now: float) -> bool:
        nonlocal passes
        passes += 1
        if passes > 1:
            return await real(config, scope, now=now)
        first_pass.set()
        await release.wait()
        return False

    looked = asyncio.Event()
    real_looks = worker._maintained_graphs

    async def signalled_looks(config: Config) -> list[GraphScope]:
        scopes = await real_looks(config)
        looked.set()
        return scopes

    with (
        patch.object(worker, "_ingest_graph", first_pass_finds_nothing),
        patch.object(worker, "_maintained_graphs", signalled_looks),
    ):
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(first_pass.wait(), timeout=10)
            _seed(config, paths, "!room:x:$t1", ["r1"], at=int(time.time()) - 10)
            looked.clear()
            wake_knowledge_graph_ingestion(after_seconds=0)
            # The look the wake starts finds the graph still in its first pass.
            await asyncio.wait_for(looked.wait(), timeout=10)
            assert graph_name in worker._looked_again
            release.set()
            await asyncio.wait_for(store.added.wait(), timeout=10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert store.sessions() == ["!room:x:$t1"]


@pytest.mark.asyncio
async def test_a_look_during_a_pass_costs_one_more_pass_and_no_more(tmp_path: Path) -> None:
    """The graph stops once the pass after the look adds nothing too, instead of passing again without end."""
    config, paths = _config(), _paths(tmp_path)
    worker = _worker(config, paths, _FakeStore())
    graph_name = graph_scope("mind", config, paths, None).graph_name
    passes = 0
    real = worker._ingest_graph

    async def looked_at_during_the_first(config: Config, scope: GraphScope, *, now: float) -> bool:
        nonlocal passes
        passes += 1
        if passes == 1:
            worker._looked_again.add(graph_name)
        return await real(config, scope, now=now)

    await worker._maintained_graphs(config)
    with patch.object(worker, "_ingest_graph", looked_at_during_the_first):
        await asyncio.wait_for(worker._fill(graph_name), timeout=10)

    assert passes == 2


@pytest.mark.asyncio
async def test_a_graph_whose_pass_loop_ended_is_filled_again_by_a_later_look(tmp_path: Path) -> None:
    """A graph that ran out of work starts a new pass loop when the worker next looks, such as on a wake."""
    config = _config(interval_seconds=3600)
    paths, store = _paths(tmp_path), _FakeStore()
    worker = _worker(config, paths, store)
    ended = asyncio.Event()
    real_fill = worker._fill

    async def signalled_fill(graph_name: str) -> None:
        await real_fill(graph_name)
        ended.set()

    with patch.object(worker, "_fill", signalled_fill):
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(ended.wait(), timeout=10)
            _seed(config, paths, "!room:x:$t1", ["r1"], at=int(time.time()) - 10)
            wake_knowledge_graph_ingestion(after_seconds=0)
            await asyncio.wait_for(store.added.wait(), timeout=10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert store.sessions() == ["!room:x:$t1"]


@pytest.mark.asyncio
async def test_a_look_lists_the_maintained_graphs_once_however_many_passes_follow(tmp_path: Path) -> None:
    """Passes reuse their look's listing, which reads every private instance's records, instead of listing again."""
    config = _config(interval_seconds=3600)
    agents = ("mind", "pal", "sage")
    for agent in agents[1:]:
        config.agents[agent] = AgentConfig(display_name=agent.title(), knowledge_graph=True)
    paths, store = _paths(tmp_path), _FakeStore()
    for agent in agents:
        _seed(config, paths, f"!room:x:${agent}", [f"r-{agent}"], at=int(time.time()) - 10, agent=agent)
    worker = _worker(config, paths, store)
    listings = 0
    real_scopes = ingestion.graph_scopes

    def counted_scopes(config: Config, runtime_paths: RuntimePaths) -> list[GraphScope]:
        nonlocal listings
        listings += 1
        return real_scopes(config, runtime_paths)

    ended = 0
    all_ended = asyncio.Event()
    real_fill = worker._fill

    async def signalled_fill(graph_name: str) -> None:
        nonlocal ended
        await real_fill(graph_name)
        ended += 1
        if ended == len(agents):
            all_ended.set()

    with patch.object(ingestion, "graph_scopes", counted_scopes), patch.object(worker, "_fill", signalled_fill):
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(all_ended.wait(), timeout=10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Each graph took a pass that added its conversation and one that found nothing more.
    assert sorted(store.sessions()) == [f"!room:x:${agent}" for agent in agents]
    assert listings == 1


@pytest.mark.asyncio
async def test_a_pass_that_waited_for_a_place_runs_on_the_settings_after_the_wait(tmp_path: Path) -> None:
    """A graph queued behind another's pass when the generation changes runs no pass on its old graph."""
    config = _config(interval_seconds=3600)
    config.agents["pal"] = AgentConfig(display_name="Pal", knowledge_graph=True)
    paths, store = _paths(tmp_path), _HeldStore(hold_all=True)
    at = int(time.time()) - 10
    _seed(config, paths, "!room:x:$m1", ["m1"], at=at)
    _seed(config, paths, "!room:x:$p1", ["p1"], at=at, agent="pal")
    changed = config.model_copy(deep=True)
    changed.knowledge_graph.generation = 1
    current = config
    old = {graph_scope(agent, config, paths, None).graph_name for agent in ("mind", "pal")}
    new = {graph_scope(agent, changed, paths, None).graph_name for agent in ("mind", "pal")}

    async def new_graphs_filled() -> None:
        while not new <= {graph for graph, _, _ in store.episodes}:
            store.added.clear()
            await store.added.wait()

    with patch.object(ingestion, "_CONCURRENT_GRAPHS", 1):
        worker = KnowledgeGraphIngestionWorker(
            runtime_paths=paths,
            config_provider=lambda: current,
            store=store,
            redacted_event_ids=_no_redactions,
        )
        task = asyncio.create_task(worker.run())
        try:
            # One graph's pass holds the only place while the other's waits for it.
            await asyncio.wait_for(store.holding.wait(), timeout=10)
            current = changed
            store.release.set()
            await asyncio.wait_for(new_graphs_filled(), timeout=10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Only the pass already running when the generation changed added to an old graph.
    assert len({graph for graph, _, _ in store.episodes if graph in old}) == 1


@pytest.mark.asyncio
async def test_graphs_that_cannot_be_listed_are_reported_and_looked_for_again(tmp_path: Path) -> None:
    """A failure to list the maintained graphs is reported and leaves the worker running."""
    config, paths = _config(interval_seconds=3600), _paths(tmp_path)
    worker = _worker(config, paths, _FakeStore())
    reported = asyncio.Event()
    real_report = ingestion._report_failure

    def unlisted(config: Config, runtime_paths: RuntimePaths) -> list[GraphScope]:
        del config, runtime_paths
        msg = "private instance records unreadable"
        raise OSError(msg)

    def signalled_report(message: str, exc: Exception, **context: str) -> None:
        real_report(message, exc, **context)
        reported.set()

    _seed(config, paths, "!room:x:$t1", ["r1"])
    with (
        patch.object(ingestion, "graph_scopes", unlisted),
        patch.object(ingestion, "_report_failure", signalled_report),
    ):
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(reported.wait(), timeout=10)
            assert "OSError" in (knowledge_graph_failure() or "")
            # A pass of a graph listed before, finishing cleanly meanwhile, does not clear the listing failure.
            assert await worker._ingest_graph(config, graph_scope("mind", config, paths, None), now=T0 + 10) is True
            assert "OSError" in (knowledge_graph_failure() or "")
            # The worker keeps running and lists the graphs again at its next look.
            reported.clear()
            wake_knowledge_graph_ingestion(after_seconds=0)
            await asyncio.wait_for(reported.wait(), timeout=10)
            assert not task.done()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Once the graphs can be listed again, the next clean pass clears it.
    _seed(config, paths, "!room:x:$t2", ["r2"], at=T0 + 20)
    await _pass(worker, config, now=T0 + 40)
    assert knowledge_graph_failure() is None


@pytest.mark.asyncio
async def test_a_graph_no_longer_maintained_stops_filling_after_its_current_pass(tmp_path: Path) -> None:
    """After a generation change, the old graph finishes the pass it is in and takes no further pass."""
    config = _config(max_sessions_per_cycle=1)
    paths, store = _paths(tmp_path), _FakeStore()
    for thread in ("$t1", "$t2"):
        _seed(config, paths, f"!room:x:{thread}", [f"r{thread}"], at=int(time.time()) - 10)
    changed = config.model_copy(deep=True)
    changed.knowledge_graph.generation = 1
    current = config
    worker = KnowledgeGraphIngestionWorker(
        runtime_paths=paths,
        config_provider=lambda: current,
        store=store,
        redacted_event_ids=_no_redactions,
    )
    real = worker._ingest_graph

    async def then_raise_the_generation(config: Config, scope: GraphScope, *, now: float) -> bool:
        nonlocal current
        progressed = await real(config, scope, now=now)
        current = changed
        return progressed

    await worker._maintained_graphs(config)
    with patch.object(worker, "_ingest_graph", then_raise_the_generation):
        await asyncio.wait_for(worker._fill(graph_scope("mind", config, paths, None).graph_name), timeout=10)

    assert len(set(store.sessions())) == 1
    # The worker looks again at once, so the next generation's graph starts without waiting the interval.
    assert worker._wake_event.is_set()


@pytest.mark.asyncio
async def test_one_graphs_clean_pass_does_not_clear_another_graphs_failure(tmp_path: Path) -> None:
    """Health keeps failing while any graph's conversation waits to retry, however cleanly the other graphs ingest."""
    config = _config()
    config.agents["pal"] = AgentConfig(display_name="Pal", knowledge_graph=True)
    paths = _paths(tmp_path)
    store = _FakeStore(failing={"!room:x:$bad"})
    _seed(config, paths, "!room:x:$bad", ["b1"])
    worker = _worker(config, paths, store)
    await _pass(worker, config, now=T0 + 10)
    assert knowledge_graph_failure() is not None

    _seed(config, paths, "!room:x:$good", ["g1"], at=T0 + 20, agent="pal")
    pal = graph_scope("pal", config, paths, None)
    assert await worker._ingest_graph(config, pal, now=T0 + 40) is True

    assert store.sessions() == ["!room:x:$good"]
    assert knowledge_graph_failure() is not None


@dataclass
class _HeldSessionStore(_FakeStore):
    """Holds one conversation's episodes until released, while the others go through."""

    held_session: str = ""
    release: asyncio.Event = field(default_factory=asyncio.Event)
    holding: asyncio.Event = field(default_factory=asyncio.Event)

    async def add_episode(self, scope: GraphScope, episode: EpisodeInput, usage: UsageTarget) -> None:
        if usage.session_id == self.held_session:
            self.holding.set()
            await self.release.wait()
        await super().add_episode(scope, episode, usage)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["extraction", "redaction check", "unreadable"])
async def test_a_failure_holds_health_while_its_graph_is_still_in_its_pass(tmp_path: Path, failure: str) -> None:
    """Another graph's clean pass ending meanwhile does not clear a failure from a pass that is still running."""
    config = _config()
    config.agents["pal"] = AgentConfig(display_name="Pal", knowledge_graph=True)
    paths = _paths(tmp_path)
    store = _HeldSessionStore(held_session="!room:x:$slow")
    if failure == "extraction":
        store.failing.add("!room:x:$bad")
    _seed(config, paths, "!room:x:$bad", ["b1"], at=T0)
    _seed(config, paths, "!room:x:$slow", ["s1"], at=T0 + 5)
    _seed(config, paths, "!room:x:$good", ["g1"], agent="pal")
    real = ingestion.session_runs

    async def redacted(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del agent_name, room_id
        if failure == "redaction check" and "$event-b1" in event_ids:
            msg = "event journal unavailable"
            raise RuntimeError(msg)
        return frozenset()

    def runs(storage: object, session_id: str) -> list[RunRecord]:
        if failure == "unreadable" and session_id == "!room:x:$bad":
            msg = "corrupt session row"
            raise ValueError(msg)
        return real(storage, session_id)

    worker = _worker(config, paths, store, redacted)
    with patch.object(ingestion, "session_runs", runs):
        mind_scope = graph_scope("mind", config, paths, None)
        mind = asyncio.create_task(worker._ingest_graph(config, mind_scope, now=T0 + 100))
        try:
            await asyncio.wait_for(store.holding.wait(), timeout=10)
            assert knowledge_graph_failure() is not None

            assert await worker._ingest_graph(config, graph_scope("pal", config, paths, None), now=T0 + 100) is True
            assert knowledge_graph_failure() is not None
        finally:
            store.release.set()
            await mind

    assert sorted(store.sessions()) == ["!room:x:$good", "!room:x:$slow"]
    assert knowledge_graph_failure() is not None


@pytest.mark.asyncio
async def test_a_graph_that_recovers_with_nothing_to_add_stops_keeping_health_failing(tmp_path: Path) -> None:
    """Once an agent's conversations can be read again, another graph's clean pass clears the failure it left."""
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
        await _pass(worker, config, now=T0 + 10)
    assert knowledge_graph_failure() is not None

    _seed(config, paths, "!room:x:$t2", ["r2"], at=T0 + 20)
    await _pass(worker, config, now=T0 + 40)

    assert store.sessions() == ["!room:x:$t1", "!room:x:$t2"]
    assert knowledge_graph_failure() is None


@pytest.mark.asyncio
async def test_a_pass_that_added_nothing_waits_for_the_interval(tmp_path: Path) -> None:
    """The worker does not spin while there is nothing to add; only a wake or the interval starts the next pass."""
    config, paths = _config(interval_seconds=3600), _paths(tmp_path)
    worker = _worker(config, paths, _FakeStore())
    passes = 0
    first_pass = asyncio.Event()

    async def empty_pass(config: Config, scope: GraphScope, *, now: float) -> bool:
        del config, scope, now
        nonlocal passes
        passes += 1
        first_pass.set()
        await asyncio.sleep(0)
        return False

    with patch.object(worker, "_ingest_graph", empty_pass):
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

    assert await _pass(_worker(config, paths, store), config, now=T0 + 10) is False
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

    await _pass(_worker(config, paths, store, redacted), config, now=T0 + 10)

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

    await _pass(_worker(config, paths, store, not_running), config, now=T0 + 10)
    assert store.episodes == []
    await _pass(_worker(config, paths, store), config, now=T0 + 20)
    assert _answers(store) == ["Mind: answer r1"]


@pytest.mark.asyncio
async def test_health_keeps_failing_while_a_conversation_waits_to_retry(tmp_path: Path) -> None:
    """Another conversation added cleanly does not clear the failure of one that is still waiting for its retry."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing={"!room:x:$bad"})
    _seed(config, paths, "!room:x:$bad", ["b1"])
    worker = _worker(config, paths, store)
    await _pass(worker, config, now=T0 + 10)

    _seed(config, paths, "!room:x:$good", ["g1"], at=T0 + 20)
    await _pass(worker, config, now=T0 + 40)

    assert store.sessions() == ["!room:x:$good"]
    assert knowledge_graph_failure() is not None


@pytest.mark.asyncio
async def test_a_failing_conversation_left_without_matrix_turns_stops_keeping_health_failing(tmp_path: Path) -> None:
    """A conversation whose failing turn is gone has nothing left to retry, so the next clean pass clears health."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing={"!room:x:$t1"})
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)
    await _pass(worker, config, now=T0 + 10)
    assert knowledge_graph_failure() is not None

    _seed(config, paths, "!room:x:$t1", ["r1"], updated_at=T0 + 4000, matrix=False)
    await _pass(worker, config, now=T0 + 5000)

    assert store.episodes == []
    assert knowledge_graph_failure() is None


@pytest.mark.asyncio
async def test_a_conversation_first_recorded_empty_still_starts_after_the_backfill_window(tmp_path: Path) -> None:
    """Turns from before a new graph's backfill window stay out, even when their conversation was first seen empty."""
    config, paths, store = _config(backfill_days=0), _paths(tmp_path), _FakeStore()
    worker = _worker(config, paths, store)
    await _pass(worker, config, now=T0 + 10)
    # A turn that has not finished, such as one still running when the graph started, is not a Matrix turn yet.
    _seed(config, paths, "!room:x:$t1", ["old"], at=T0 + 5, updated_at=T0 + 20, matrix=False)
    await _pass(worker, config, now=T0 + 30)

    _seed(config, paths, "!room:x:$t1", ["old", "new"], at=T0 + 5, spacing=35)
    await _pass(worker, config, now=T0 + 60)

    assert _answers(store) == ["Mind: answer new"]


@pytest.mark.asyncio
async def test_a_graph_no_longer_maintained_stops_keeping_health_failing(tmp_path: Path) -> None:
    """After an embedder change, the old graph's failing episode no longer holds the failure once the new one is fine."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing={"!room:x:$t1"})
    _seed(config, paths, "!room:x:$t1", ["r1"])
    worker = _worker(config, paths, store)
    await _pass(worker, config, now=T0 + 10)
    assert knowledge_graph_failure() is not None

    store.failing.clear()
    changed = config.model_copy(deep=True)
    changed.memory.embedder.config.dimensions = 256
    await _pass(worker, changed, now=T0 + 20)

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
    assert await _pass(_worker(config, paths, store, only_pal_runs), config, now=T0 + 10) is False
    assert knowledge_graph_failure() is not None

    _seed(config, paths, "!room:x:$pal", ["p1"], agent="pal")
    assert await _pass(_worker(config, paths, store, only_pal_runs), config, now=T0 + 20) is True
    assert store.sessions() == ["!room:x:$pal"]


@pytest.mark.asyncio
async def test_a_failing_tool_result_is_dropped_while_the_conversation_keeps_succeeding(tmp_path: Path) -> None:
    """New turns that extract fine do not reset the count of the tool results that keep failing behind them."""
    config, paths = _config(), _paths(tmp_path)
    store = _FakeStore(failing_text="Issue for r0")
    worker = _worker(config, paths, store)

    for day in range(7):
        _seed(config, paths, "!room:x:$t1", [f"r{index}" for index in range(day + 1)], spacing=7200)
        await _pass(worker, config, now=T0 + day * 7200 + 100)

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
        await _pass(_worker(config, paths, store), config, now=T0 + 10)

    assert rendered_on
    assert all(thread is not threading.main_thread() for thread in rendered_on)
