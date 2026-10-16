"""Background ingestion: the finished turns of graph-enabled agents become graph episodes.

Stored sessions are the source of truth; a per-graph state file records how far each conversation has been ingested,
so a restart resumes where the last pass stopped and changing the embedder, which names a new graph, starts over.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from mindroom.automations.threads import automation_threads
from mindroom.durable_write import write_json_file_durable
from mindroom.knowledge_graph.episodes import classify_thread, render_episodes, run_key
from mindroom.knowledge_graph.scope import graph_scopes
from mindroom.knowledge_graph.session_runs import session_runs, updated_sessions
from mindroom.knowledge_graph.store import record_knowledge_graph_failure
from mindroom.knowledge_graph.usage import UsageTarget
from mindroom.logging_config import get_logger
from mindroom.session_storage.agent_storage import create_session_storage

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agno.run.agent import RunOutput

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge_graph.scope import GraphScope
    from mindroom.knowledge_graph.store import GraphStore

logger = get_logger(__name__)

_DAY_SECONDS = 86_400
_IDLE_POLL_SECONDS = 30
_FIRST_RETRY_SECONDS = 60
_MAX_RETRY_SECONDS = 3600
_WAKE_EVENTS: set[asyncio.Event] = set()


class _SessionState(TypedDict):
    updated_at: int
    marker: list[int | str] | None
    failures: int
    retry_at: float


class _GraphState(TypedDict):
    started_at: float
    sessions: dict[str, _SessionState]


@dataclass(frozen=True)
class _DueSession:
    session_id: str
    updated_at: int
    runs: list[RunOutput]


def wake_knowledge_graph_ingestion(*, after_seconds: float = 0) -> None:
    """Run an ingestion pass after ``after_seconds`` instead of at the next interval; call on the event loop."""

    def wake() -> None:
        for event in _WAKE_EVENTS:
            event.set()

    asyncio.get_running_loop().call_later(after_seconds, wake)


@dataclass
class KnowledgeGraphIngestionWorker:
    """Periodically ingest the new finished turns of every graph-enabled agent and private instance."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    store: GraphStore
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def stop(self) -> None:
        """Ask the loop to end after the current pass."""
        self._stop_event.set()
        self._wake_event.set()

    async def run(self) -> None:
        """Run ingestion passes until stopped."""
        _WAKE_EVENTS.add(self._wake_event)
        try:
            await self._loop()
        finally:
            _WAKE_EVENTS.discard(self._wake_event)

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            # Cleared before the pass, so a wake that arrives during it starts the next one at once.
            self._wake_event.clear()
            config = self.config_provider()
            interval = _IDLE_POLL_SECONDS
            if config is not None and config.knowledge_graph_agents():
                await self._run_cycle(config, now=time.time())
                interval = config.knowledge_graph.ingest.interval_seconds
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _run_cycle(self, config: Config, *, now: float) -> None:
        """Ingest up to ``max_sessions_per_cycle`` idle conversations with new turns."""
        budget = config.knowledge_graph.ingest.max_sessions_per_cycle
        automation_thread_ids = automation_threads(self.runtime_paths)
        outcomes: list[bool] = []
        for scope in graph_scopes(config, self.runtime_paths):
            if budget <= 0:
                break
            state = self._load_state(scope.graph_name, now)
            due = await asyncio.to_thread(self._due_sessions, config, scope, state, now, budget)
            budget -= len(due)
            outcomes.extend(
                [await self._ingest(config, scope, state, session, automation_thread_ids, now) for session in due],
            )
        # Health reports a failure until a pass ingests something and nothing in it fails.
        if outcomes and all(outcomes):
            record_knowledge_graph_failure(None)

    def _due_sessions(
        self,
        config: Config,
        scope: GraphScope,
        state: _GraphState,
        now: float,
        limit: int,
    ) -> list[_DueSession]:
        settings = config.knowledge_graph.ingest
        storage = create_session_storage(
            scope.agent_name,
            config,
            self.runtime_paths,
            execution_identity=scope.execution_identity,
        )
        try:
            since = int(state["started_at"]) - settings.backfill_days * _DAY_SECONDS
            due: list[_DueSession] = []
            for session_id, updated_at in updated_sessions(storage, scope.agent_name, since=since):
                if len(due) >= limit:
                    break
                entry = state["sessions"].get(session_id)
                if entry is not None and (entry["updated_at"] >= updated_at or entry["retry_at"] > now):
                    continue
                if now - updated_at < settings.idle_seconds:
                    continue
                due.append(_DueSession(session_id, updated_at, session_runs(storage, session_id)))
            return due
        finally:
            storage.close()

    async def _ingest(
        self,
        config: Config,
        scope: GraphScope,
        state: _GraphState,
        session: _DueSession,
        automation_thread_ids: set[str],
        now: float,
    ) -> bool:
        """Ingest one conversation's new turns and return whether it succeeded."""
        entry = state["sessions"].setdefault(
            session.session_id,
            {"updated_at": 0, "marker": None, "failures": 0, "retry_at": 0},
        )
        marker = (int(entry["marker"][0]), str(entry["marker"][1])) if entry["marker"] is not None else None
        new_runs = [run for run in session.runs if marker is None or run_key(run) > marker]
        chunks = render_episodes(
            new_runs,
            kind=classify_thread(session.runs, automation_thread_ids),
            agent_name=scope.agent_name,
            settings=config.knowledge_graph.ingest,
        )
        usage = UsageTarget(scope.agent_name, session.session_id, scope.execution_identity)
        try:
            for chunk in chunks:
                for episode in chunk.episodes:
                    await self.store.add_episode(scope, episode, usage)
                entry["marker"] = list(chunk.last_run)
                self._save_state(scope.graph_name, state)
        except Exception as exc:
            entry["failures"] += 1
            entry["retry_at"] = now + min(_FIRST_RETRY_SECONDS * 2 ** (entry["failures"] - 1), _MAX_RETRY_SECONDS)
            self._save_state(scope.graph_name, state)
            detail = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning("Knowledge graph ingestion failed", agent=scope.agent_name, error=detail)
            record_knowledge_graph_failure(detail)
            return False
        entry.update(updated_at=session.updated_at, failures=0, retry_at=0)
        self._save_state(scope.graph_name, state)
        return True

    def _state_path(self, graph_name: str) -> Path:
        return self.runtime_paths.storage_root / "knowledge_graph" / "ingestion" / f"{graph_name}.json"

    def _load_state(self, graph_name: str, now: float) -> _GraphState:
        try:
            state = json.loads(self._state_path(graph_name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"started_at": now, "sessions": {}}
        except ValueError:
            # Starting over re-extracts recent conversations, which Graphiti merges with what it already holds.
            logger.warning("Knowledge graph ingestion state was unreadable; starting over", graph=graph_name)
            return {"started_at": now, "sessions": {}}
        return state

    def _save_state(self, graph_name: str, state: _GraphState) -> None:
        path = self._state_path(graph_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_file_durable(path, state)
