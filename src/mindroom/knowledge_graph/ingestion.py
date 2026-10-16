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

from agno.run.base import RunStatus

from mindroom.automations.threads import automation_threads
from mindroom.durable_write import write_json_file_durable
from mindroom.knowledge_graph.episodes import RenderedEpisode, classify_thread, render_episodes, run_key
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
# An episode whose extraction output is unusable this often, or that has failed for this long, is dropped, so it
# cannot cost model calls forever; an outage shorter than that loses nothing.
_MAX_BAD_OUTPUTS = 5
_MAX_FAILING_SECONDS = 3 * 86_400
# Episodes one conversation adds per pass, so a long backlog cannot hold up every other conversation.
_MAX_EPISODES_PER_PASS = 20
_EPISODE_KINDS = ("conversation", "tool_results")
_WAKE_EVENTS: set[asyncio.Event] = set()


class _SessionState(TypedDict):
    updated_at: int
    # The last run each episode kind has ingested, so a failing kind never repeats the other.
    markers: dict[str, list[int | str]]
    # Consecutive failures, which set the backoff.
    failures: int
    # Consecutive failures of the current episode whose model output could not be used, which drop it.
    bad_outputs: int
    # When the current episode started failing, so one that never succeeds is dropped after a while.
    failing_since: float
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


def _new_session_state() -> _SessionState:
    return {"updated_at": 0, "markers": {}, "failures": 0, "bad_outputs": 0, "failing_since": 0, "retry_at": 0}


def _approval_window_seconds(config: Config) -> float:
    """Return how long a turn can wait for approval before every approval it could get has expired."""
    approval = config.tool_approval
    days = max([approval.timeout_days, *(rule.timeout_days for rule in approval.rules if rule.timeout_days)])
    return days * _DAY_SECONDS


def _finished_runs(runs: list[RunOutput], now: float, approval_window: float) -> list[RunOutput]:
    """Return the finished runs before the first one that can still finish later.

    A turn waiting for approval finishes in place with its original, earlier creation time, so nothing after it is
    ingested yet; once its approval window has passed it can no longer finish and is skipped.
    """
    finished: list[RunOutput] = []
    for run in runs:
        if run.status == RunStatus.paused:
            if now - (run.created_at or 0) < approval_window:
                break
            continue
        finished.append(run)
    return finished


def _start_after_backfill_window(entry: _SessionState, runs: list[RunOutput], *, cutoff: float) -> None:
    """Mark the runs before the backfill window as done, so a long conversation adds only its recent turns."""
    older = [run for run in runs if (run.created_at or 0) < cutoff]
    if older:
        key = list(run_key(older[-1]))
        entry["markers"] = dict.fromkeys(_EPISODE_KINDS, key)


def _marker(entry: _SessionState, kind: str) -> tuple[int, str] | None:
    marker = entry["markers"].get(kind)
    return (int(marker[0]), str(marker[1])) if marker is not None else None


def _report_failure(message: str, exc: Exception, **context: str) -> None:
    detail = f"{type(exc).__name__}: {str(exc)[:200]}"
    logger.warning(message, error=detail, **context)
    record_knowledge_graph_failure(detail)


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
                try:
                    await self._run_cycle(config, now=time.time())
                except Exception as exc:
                    _report_failure("Knowledge graph ingestion pass failed", exc)
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
            # One agent or private instance whose conversations cannot be read must not stop the others.
            try:
                state = self._load_state(scope.graph_name, now)
                due = await asyncio.to_thread(self._due_sessions, config, scope, state, now, budget)
                budget -= len(due)
                outcomes.extend(
                    [await self._ingest(config, scope, state, session, automation_thread_ids, now) for session in due],
                )
            except Exception as exc:
                _report_failure("Knowledge graph ingestion failed for an agent", exc, agent=scope.agent_name)
                outcomes.append(False)
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
        """Ingest a bounded number of one conversation's new episodes and return whether none failed or was dropped."""
        settings = config.knowledge_graph.ingest
        first_sight = session.session_id not in state["sessions"]
        entry = state["sessions"].setdefault(session.session_id, _new_session_state())
        finished = _finished_runs(session.runs, now, _approval_window_seconds(config))
        if first_sight:
            _start_after_backfill_window(
                entry,
                finished,
                cutoff=state["started_at"] - settings.backfill_days * _DAY_SECONDS,
            )
        thread_kind = classify_thread(session.runs, automation_thread_ids)
        requester_id = next(
            (requester for run in reversed(session.runs) if (requester := (run.metadata or {}).get("requester_id"))),
            None,
        )
        usage = UsageTarget(scope.agent_name, session.session_id, scope.execution_identity, requester_id)
        clean = True
        completed = 0
        for kind in _EPISODE_KINDS:
            marker = _marker(entry, kind)
            new_runs = [run for run in finished if marker is None or run_key(run) > marker]
            for rendered in render_episodes(
                new_runs,
                thread_kind=thread_kind,
                episode_kind=kind,
                agent_name=scope.agent_name,
                settings=settings,
            ):
                added = await self._add(scope, state, entry, kind, rendered, usage, now)
                if added is None:
                    return False
                clean = clean and added
                completed += rendered.last_run is not None
                # A long backlog spreads over passes; the conversation stays due because its update time is unchanged.
                if completed >= _MAX_EPISODES_PER_PASS:
                    return clean
            if new_runs:
                entry["markers"][kind] = list(run_key(new_runs[-1]))
        entry.update(updated_at=session.updated_at, failures=0, bad_outputs=0, failing_since=0, retry_at=0)
        self._save_state(scope.graph_name, state)
        return clean

    async def _add(
        self,
        scope: GraphScope,
        state: _GraphState,
        entry: _SessionState,
        kind: str,
        rendered: RenderedEpisode,
        usage: UsageTarget,
        now: float,
    ) -> bool | None:
        """Add one episode; return True when added, False when dropped, and None when it waits for a later pass."""
        try:
            await self.store.add_episode(scope, rendered.episode, usage)
            dropped = False
        except Exception as exc:
            entry["failures"] += 1
            # Unusable model output, such as JSON that does not match the schema, raises ValueError.
            entry["bad_outputs"] = entry["bad_outputs"] + 1 if isinstance(exc, ValueError) else 0
            entry["failing_since"] = entry["failing_since"] or now
            _report_failure("Knowledge graph ingestion failed", exc, agent=scope.agent_name)
            if entry["bad_outputs"] < _MAX_BAD_OUTPUTS and now - entry["failing_since"] < _MAX_FAILING_SECONDS:
                backoff = _FIRST_RETRY_SECONDS * 2 ** (entry["failures"] - 1)
                entry["retry_at"] = now + min(backoff, _MAX_RETRY_SECONDS)
                self._save_state(scope.graph_name, state)
                return None
            logger.exception(
                "Knowledge graph dropped an episode it could not extract",
                agent=scope.agent_name,
                episode=rendered.episode.name,
            )
            dropped = True
        entry.update(failures=0, bad_outputs=0, failing_since=0)
        if rendered.last_run is not None:
            entry["markers"][kind] = list(rendered.last_run)
        self._save_state(scope.graph_name, state)
        return not dropped

    def _state_path(self, graph_name: str) -> Path:
        return self.runtime_paths.storage_root / "knowledge_graph" / "ingestion" / f"{graph_name}.json"

    def _load_state(self, graph_name: str, now: float) -> _GraphState:
        try:
            return json.loads(self._state_path(graph_name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except ValueError:
            # Starting over re-extracts recent conversations, which Graphiti merges with what it already holds.
            logger.warning("Knowledge graph ingestion state was unreadable; starting over", graph=graph_name)
        # Saved at once, so the backfill window stays anchored to when the graph started.
        state: _GraphState = {"started_at": now, "sessions": {}}
        self._save_state(graph_name, state)
        return state

    def _save_state(self, graph_name: str, state: _GraphState) -> None:
        path = self._state_path(graph_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_file_durable(path, state)
