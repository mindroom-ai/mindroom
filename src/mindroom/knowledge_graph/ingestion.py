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
from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.history.storage import run_event_ids
from mindroom.knowledge_graph.episodes import (
    RUN_DONE,
    Progress,
    RenderedEpisode,
    classify_thread,
    render_episodes,
    run_key,
)
from mindroom.knowledge_graph.scope import graph_scopes
from mindroom.knowledge_graph.session_runs import session_runs, updated_sessions
from mindroom.knowledge_graph.store import UnusableExtractionError, record_knowledge_graph_failure
from mindroom.knowledge_graph.usage import UsageTarget
from mindroom.logging_config import get_logger
from mindroom.session_storage.agent_storage import create_session_storage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agno.run.agent import RunOutput

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge_graph.scope import GraphScope
    from mindroom.knowledge_graph.store import GraphStore

logger = get_logger(__name__)

# Which of a room's events an agent's journal saw redacted, or None while that agent's bot is not running.
type RedactedEventIds = Callable[[str, str, tuple[str, ...]], Awaitable[frozenset[str] | None]]

_DAY_SECONDS = 86_400
_IDLE_POLL_SECONDS = 30
_FIRST_RETRY_SECONDS = 60
_MAX_RETRY_SECONDS = 3600
_APPROVAL_GRACE_SECONDS = 3600
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
    # The failing episode, as its kind and where that kind stopped, which stays put while it fails; empty when none.
    failing_piece: list[int | str]
    retry_at: float
    # When a turn holding the conversation back stops waiting for approval, or 0.
    recheck_at: float


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
    return {
        "updated_at": 0,
        "markers": {},
        "failures": 0,
        "bad_outputs": 0,
        "failing_since": 0,
        "failing_piece": [],
        "retry_at": 0,
        "recheck_at": 0,
    }


def _approval_window_seconds(config: Config) -> float:
    """Return how long a turn can wait for approval before every approval it could get has expired and resumed it."""
    approval = config.tool_approval
    days = max([approval.timeout_days, *(rule.timeout_days for rule in approval.rules if rule.timeout_days)])
    # The card's expiry counts from when it was posted, a little after the newest message, and then resumes the turn.
    return days * _DAY_SECONDS + _APPROVAL_GRACE_SECONDS


def _finished_runs(
    runs: list[RunOutput],
    now: float,
    approval_window: float,
) -> tuple[list[RunOutput], float | None]:
    """Return the finished runs before the first one that can still finish later, and when that one stops waiting.

    A turn waiting for approval finishes in place with its original, earlier creation time, so nothing after it is
    ingested yet; once its approval window has passed it can no longer finish and is skipped.
    """
    finished: list[RunOutput] = []
    for run in runs:
        if run.status == RunStatus.paused:
            # Its newest message is the call waiting for approval, whose card expires counting from when it was posted.
            asked_at = max([run.created_at or 0, *((message.created_at or 0) for message in run.messages or [])])
            expires_at = asked_at + approval_window
            if now < expires_at:
                return finished, expires_at
            continue
        finished.append(run)
    return finished, None


def _start_after_backfill_window(entry: _SessionState, runs: list[RunOutput], *, cutoff: float) -> None:
    """Mark the runs before the backfill window as done, so a long conversation adds only its recent turns."""
    older = [run for run in runs if (run.created_at or 0) < cutoff]
    if older:
        done = [*run_key(older[-1]), RUN_DONE]
        entry["markers"] = {kind: list(done) for kind in _EPISODE_KINDS}


def _marker(entry: _SessionState, kind: str) -> Progress | None:
    marker = entry["markers"].get(kind)
    return (int(marker[0]), str(marker[1]), int(marker[2])) if marker is not None else None


def _report_failure(message: str, exc: Exception, **context: str) -> None:
    logger.warning(message, error=f"{type(exc).__name__}: {str(exc)[:200]}", **context)
    # Health is unauthenticated and error text can quote conversation content, so it gets only the failure's kind.
    record_knowledge_graph_failure(f"{message}: {type(exc).__name__}")


@dataclass
class KnowledgeGraphIngestionWorker:
    """Periodically ingest the new finished turns of every graph-enabled agent and private instance."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    store: GraphStore
    redacted_event_ids: RedactedEventIds
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _passes: int = field(default=0, init=False)

    async def run(self) -> None:
        """Run ingestion passes until cancelled."""
        _WAKE_EVENTS.add(self._wake_event)
        try:
            await self._loop()
        finally:
            _WAKE_EVENTS.discard(self._wake_event)

    async def _loop(self) -> None:
        while True:
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
        # These read files, including every private instance's records, so they stay off the event loop.
        automation_thread_ids = await asyncio.to_thread(automation_threads, self.runtime_paths)
        entity_names = await asyncio.to_thread(prepared_entity_names_by_user_id, config, self.runtime_paths)
        outcomes: list[bool] = []
        scopes = await asyncio.to_thread(graph_scopes, config, self.runtime_paths)
        # Each pass starts with the next scope, so one with a long backlog cannot take every pass's budget.
        start = self._passes % len(scopes) if scopes else 0
        self._passes += 1
        for scope in [*scopes[start:], *scopes[:start]]:
            if budget <= 0:
                break
            # One agent or private instance whose conversations cannot be read must not stop the others.
            try:
                state = await asyncio.to_thread(self._load_state, scope.graph_name, now)
                due, readable = await asyncio.to_thread(self._due_sessions, config, scope, state, now, budget)
                budget -= len(due)
                if not readable:
                    outcomes.append(False)
                outcomes.extend(
                    [
                        await self._ingest(
                            config,
                            scope,
                            state,
                            session,
                            automation_thread_ids,
                            entity_names.get,
                            now,
                        )
                        for session in due
                    ],
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
    ) -> tuple[list[_DueSession], bool]:
        """Return up to ``limit`` idle conversations with new turns, and whether every conversation could be read."""
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
            readable = True
            for session_id, updated_at in updated_sessions(storage, scope.agent_name, since=since):
                if len(due) >= limit:
                    break
                entry = state["sessions"].get(session_id)
                if entry is not None and (
                    entry["retry_at"] > now
                    or (updated_at <= entry["updated_at"] and not 0 < entry["recheck_at"] <= now)
                ):
                    continue
                if now - updated_at < settings.idle_seconds:
                    continue
                try:
                    runs = session_runs(storage, session_id)
                except Exception as exc:
                    # One conversation that cannot be read must not stop the agent's others.
                    _report_failure(
                        "Knowledge graph ingestion could not read a conversation",
                        exc,
                        agent=scope.agent_name,
                    )
                    readable = False
                    continue
                due.append(_DueSession(session_id, updated_at, runs))
            return due, readable
        finally:
            storage.close()

    async def _ingest(
        self,
        config: Config,
        scope: GraphScope,
        state: _GraphState,
        session: _DueSession,
        automation_thread_ids: set[str],
        agent_for_sender: Callable[[str], str | None],
        now: float,
    ) -> bool:
        """Ingest a bounded number of one conversation's new episodes and return whether none failed or was dropped."""
        settings = config.knowledge_graph.ingest
        finished, blocked_until = _finished_runs(session.runs, now, _approval_window_seconds(config))
        redacted = await self._redacted(scope.agent_name, finished)
        if redacted is None:
            # Its agent cannot say yet what was redacted, so the conversation is looked at again on the next pass.
            return True
        # A redaction removes a turn from stored history only when the agent next replies there, so it is skipped here.
        finished = [run for run in finished if not run_event_ids(run) & redacted]
        first_sight = session.session_id not in state["sessions"]
        entry = state["sessions"].setdefault(session.session_id, _new_session_state())
        if first_sight:
            _start_after_backfill_window(
                entry,
                finished,
                cutoff=state["started_at"] - settings.backfill_days * _DAY_SECONDS,
            )
        thread_kind = classify_thread(session.runs, automation_thread_ids)
        clean = True
        added = 0
        for kind in _EPISODE_KINDS:
            marker = _marker(entry, kind)
            # A run whose pieces are partly done is rendered again, and its done pieces are skipped below.
            new_runs = [run for run in finished if marker is None or (*run_key(run), RUN_DONE) > marker]
            # Rendering redacts every tool result of the backlog, which must not stall the event loop.
            episodes = await asyncio.to_thread(
                render_episodes,
                new_runs,
                thread_kind=thread_kind,
                episode_kind=kind,
                agent_name=scope.agent_name,
                settings=settings,
                agent_for_sender=agent_for_sender,
            )
            for rendered in episodes:
                if marker is not None and rendered.progress <= marker:
                    continue
                usage = UsageTarget(
                    scope.agent_name,
                    session.session_id,
                    scope.execution_identity,
                    rendered.requester_id,
                )
                outcome = await self._add(scope, state, entry, kind, rendered, usage, now)
                if outcome is None:
                    return False
                clean = clean and outcome
                added += 1
                # A long backlog spreads over passes; the conversation stays due because its update time is unchanged.
                if added >= _MAX_EPISODES_PER_PASS:
                    return clean
            if new_runs:
                entry["markers"][kind] = [*run_key(new_runs[-1]), RUN_DONE]
        # Held back by a turn waiting for approval, it is looked at again when those approvals expire, even without news.
        entry.update(
            updated_at=session.updated_at,
            recheck_at=blocked_until or 0,
            failures=0,
            bad_outputs=0,
            failing_since=0,
            failing_piece=[],
            retry_at=0,
        )
        await self._persist(scope.graph_name, state)
        return clean

    async def _redacted(self, agent_name: str, runs: list[RunOutput]) -> frozenset[str] | None:
        """Return the events the runs derive from that were redacted, or None while the agent cannot tell."""
        by_room: dict[str, set[str]] = {}
        for run in runs:
            by_room.setdefault(str((run.metadata or {}).get("room_id")), set()).update(run_event_ids(run))
        redacted: set[str] = set()
        for room_id, event_ids in by_room.items():
            if not event_ids:
                continue
            found = await self.redacted_event_ids(agent_name, room_id, tuple(sorted(event_ids)))
            if found is None:
                return None
            redacted |= found
        return frozenset(redacted)

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
        piece = [kind, *entry["markers"].get(kind, [])]
        try:
            await self.store.add_episode(scope, rendered.episode, usage)
            dropped = False
        except Exception as exc:
            if entry["failing_piece"] != piece:
                # The counts belong to one episode, so another kind's successes never reset them.
                entry.update(failures=0, bad_outputs=0, failing_since=0, failing_piece=piece)
            entry["failures"] += 1
            # Only output the model got wrong counts here; a setup error such as a bad embedder waits like an outage.
            entry["bad_outputs"] = entry["bad_outputs"] + 1 if isinstance(exc, UnusableExtractionError) else 0
            entry["failing_since"] = entry["failing_since"] or now
            _report_failure("Knowledge graph ingestion failed", exc, agent=scope.agent_name)
            if entry["bad_outputs"] < _MAX_BAD_OUTPUTS and now - entry["failing_since"] < _MAX_FAILING_SECONDS:
                backoff = _FIRST_RETRY_SECONDS * 2 ** (entry["failures"] - 1)
                entry["retry_at"] = now + min(backoff, _MAX_RETRY_SECONDS)
                await self._persist(scope.graph_name, state)
                return None
            logger.exception(
                "Knowledge graph dropped an episode it could not extract",
                agent=scope.agent_name,
                episode=rendered.episode.name,
            )
            dropped = True
        # Every piece records its progress, added or dropped, so a retry never repeats it.
        if dropped or entry["failing_piece"] == piece:
            entry.update(failures=0, bad_outputs=0, failing_since=0, failing_piece=[])
        entry["markers"][kind] = list(rendered.progress)
        await self._persist(scope.graph_name, state)
        return not dropped

    async def _persist(self, graph_name: str, state: _GraphState) -> None:
        await asyncio.to_thread(self._save_state, graph_name, state)

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
        write_json_file_durable(self._state_path(graph_name), state)
