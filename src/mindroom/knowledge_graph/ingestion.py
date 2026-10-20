"""Background ingestion: the finished turns of graph-enabled agents become graph episodes.

Stored sessions are the source of truth; a per-graph state file records how far each conversation has been ingested,
so a restart resumes where the last pass stopped and changing the embedder or the generation, which names a new graph,
starts over.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

from mindroom.automations.threads import automation_threads
from mindroom.durable_write import write_json_file_durable
from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.history.session_runs import finished_runs, run_key, session_runs, unredacted_runs, updated_sessions
from mindroom.knowledge_graph.episodes import (
    RUN_DONE,
    Progress,
    RenderedEpisode,
    classify_thread,
    newest_display_names,
    render_episodes,
)
from mindroom.knowledge_graph.names import entity_names
from mindroom.knowledge_graph.scope import graph_scopes
from mindroom.knowledge_graph.store import UnusableExtractionError, record_knowledge_graph_failure
from mindroom.knowledge_graph.usage import UsageTarget
from mindroom.logging_config import get_logger
from mindroom.session_storage.agent_storage import create_session_storage

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agno.run.agent import RunOutput

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.session_runs import RedactedEventIds
    from mindroom.knowledge_graph.names import EntityNames
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
# Graphs ingested at once. Each still adds one episode at a time, because Graphiti resolves an episode's entities against
# what its graph already holds, and each episode already makes many model calls at once.
_CONCURRENT_GRAPHS = 4
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


def _start_after_backfill_window(entry: _SessionState, runs: list[RunOutput], *, cutoff: float) -> None:
    """Mark the runs before the backfill window as done, so a long conversation adds only its recent turns."""
    older = [run for run in runs if (run.created_at or 0) < cutoff]
    if older:
        done = [*run_key(older[-1]), RUN_DONE]
        entry["markers"] = {kind: list(done) for kind in _EPISODE_KINDS}


def _finish(entry: _SessionState, updated_at: int, recheck_at: float) -> None:
    """Record every new turn as done, so the conversation waits for news or for ``recheck_at``."""
    entry.update(
        updated_at=updated_at,
        recheck_at=recheck_at,
        failures=0,
        bad_outputs=0,
        failing_since=0,
        failing_piece=[],
        retry_at=0,
    )


def _back_off(entry: _SessionState, now: float) -> None:
    """Count one more consecutive failure and wait longer before trying the conversation again."""
    entry["failures"] += 1
    entry["retry_at"] = now + min(_FIRST_RETRY_SECONDS * 2 ** (entry["failures"] - 1), _MAX_RETRY_SECONDS)


def _marker(entry: _SessionState, kind: str) -> Progress | None:
    marker = entry["markers"].get(kind)
    return (int(marker[0]), str(marker[1]), int(marker[2])) if marker is not None else None


def _report_failure(message: str, exc: Exception, **context: str) -> None:
    logger.warning(message, error=f"{type(exc).__name__}: {str(exc)[:200]}", **context)
    # Health is unauthenticated and error text can quote conversation content, so it gets only the failure's kind.
    record_knowledge_graph_failure(f"{message}: {type(exc).__name__}")


@dataclass
class KnowledgeGraphIngestionWorker:
    """Keep the graph of every graph-enabled agent and private instance filled with its new finished turns."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    store: GraphStore
    redacted_event_ids: RedactedEventIds
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(_CONCURRENT_GRAPHS), init=False)
    # Whether each maintained graph's last pass was clean, kept only after a pass that tried something, so one graph's
    # clean pass never hides another's failure.
    _clean: dict[str, bool] = field(default_factory=dict, init=False)
    # Graphs a look found still in a pass, which take one more pass before they stop, so a wake is never lost.
    _looked_again: set[str] = field(default_factory=set, init=False)

    async def run(self) -> None:
        """Start a pass loop for every maintained graph that is not filling, every interval or wake, until cancelled."""
        _WAKE_EVENTS.add(self._wake_event)
        fills: dict[str, asyncio.Task[None]] = {}
        try:
            while True:
                # Cleared before looking, so a wake that arrives meanwhile looks again at once.
                self._wake_event.clear()
                config = self.config_provider()
                interval = _IDLE_POLL_SECONDS
                if config is not None and config.knowledge_graph_agents():
                    interval = config.knowledge_graph.ingest.interval_seconds
                    try:
                        scopes = await self._maintained_graphs(config)
                    except Exception as exc:
                        _report_failure("Knowledge graph ingestion pass failed", exc)
                        scopes = []
                    fills = {name: fill for name, fill in fills.items() if not fill.done()}
                    for scope in scopes:
                        if scope.graph_name in fills:
                            self._looked_again.add(scope.graph_name)
                        else:
                            fills[scope.graph_name] = asyncio.create_task(self._fill(scope.graph_name))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake_event.wait(), timeout=interval)
        finally:
            _WAKE_EVENTS.discard(self._wake_event)
            for fill in fills.values():
                fill.cancel()
            await asyncio.gather(*fills.values(), return_exceptions=True)

    async def _fill(self, graph_name: str) -> None:
        """Run one graph's passes back to back, so a long backlog in another graph never holds it up.

        Stop after a pass that added nothing, unless a look arrived during it, or once the graph is no longer maintained,
        for example after an embedder or generation change, leaving the next look to ``run``.
        """
        while True:
            async with self._slots:
                # Read once a place is free, so a pass that waited for one never runs on settings changed meanwhile.
                self._looked_again.discard(graph_name)
                config = self.config_provider()
                if config is None:
                    return
                try:
                    scopes = await self._maintained_graphs(config)
                except Exception as exc:
                    _report_failure("Knowledge graph ingestion pass failed", exc)
                    return
                scope = next((scope for scope in scopes if scope.graph_name == graph_name), None)
                if scope is None:
                    # The config that replaced this graph maintains another, such as its next generation, which starts.
                    self._wake_event.set()
                    return
                progressed = await self._ingest_graph(config, scope, now=time.time())
            if not progressed and graph_name not in self._looked_again:
                return

    async def _maintained_graphs(self, config: Config) -> list[GraphScope]:
        """Return the graphs the current config maintains, and stop counting the others toward health."""
        # This reads every private instance's records, so it stays off the event loop.
        scopes = await asyncio.to_thread(graph_scopes, config, self.runtime_paths)
        names = {scope.graph_name for scope in scopes}
        self._clean = {name: clean for name, clean in self._clean.items() if name in names}
        return scopes

    def _hold_failure(self, scope: GraphScope, message: str, exc: Exception) -> None:
        """Report a graph's failure and keep health failing until its next clean pass, however others' passes end."""
        self._clean[scope.graph_name] = False
        _report_failure(message, exc, agent=scope.agent_name)

    async def _ingest_graph(self, config: Config, scope: GraphScope, *, now: float) -> bool:
        """Ingest up to ``max_sessions_per_cycle`` of one graph's idle conversations with new turns, in a held place.

        Return whether any conversation added something, so the graph's backlog is worked through without waiting.
        """
        outcomes: list[bool] = []
        progressed: list[bool] = []
        # A pass that fails holds its graph's health failing and leaves its pass loop running.
        try:
            # These read files, so they stay off the event loop.
            automation_thread_ids = await asyncio.to_thread(automation_threads, self.runtime_paths)
            agent_keys = await asyncio.to_thread(prepared_entity_names_by_user_id, config, self.runtime_paths)
            names = entity_names(config, agent_keys)
            budget = config.knowledge_graph.ingest.max_sessions_per_cycle
            state = await asyncio.to_thread(self._load_state, scope.graph_name, now)
            due, changed, unreadable = await asyncio.to_thread(
                self._due_sessions,
                config,
                scope,
                state,
                now,
                budget,
            )
            for exc in unreadable:
                self._hold_failure(scope, "Knowledge graph ingestion could not read a conversation", exc)
            if changed:
                await self._persist(scope.graph_name, state)
                # A conversation finished while listing is a clean outcome; one it could not read saved its failure.
                outcomes.append(True)
            results = [
                await self._ingest(config, scope, state, session, automation_thread_ids, names, now) for session in due
            ]
            # A conversation that added nothing, such as one whose agent is not running or whose first episode
            # failed, is looked at again later and is not counted.
            progressed = [result for result in results if result is not None]
            outcomes.extend(progressed)
            # A conversation's failure is saved with it, so health stays failing until it succeeds, even across
            # passes in which it is not due.
            if any(entry["failures"] for entry in state["sessions"].values()):
                outcomes.append(False)
        except Exception as exc:
            self._hold_failure(scope, "Knowledge graph ingestion failed for an agent", exc)
            outcomes.append(False)
            progressed = []
        # A pass that tried nothing drops the graph's outcome, so the graph no longer holds health failing.
        if outcomes:
            self._clean[scope.graph_name] = all(outcomes)
        else:
            self._clean.pop(scope.graph_name, None)
        # Health clears at the end of any pass once every maintained graph's last pass that tried something was clean;
        # a conversation still waiting for its retry makes each of its graph's passes unclean.
        if self._clean and all(self._clean.values()):
            record_knowledge_graph_failure(None)
        return bool(progressed)

    def _due_sessions(
        self,
        config: Config,
        scope: GraphScope,
        state: _GraphState,
        now: float,
        limit: int,
    ) -> tuple[list[_DueSession], bool, list[Exception]]:
        """Return up to ``limit`` idle conversations with new turns, whether listing changed the state, and read errors.

        The caller reports each conversation it could not read on the event loop, where its graph's health is held.
        """
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
            changed = False
            unreadable: list[Exception] = []
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
                    unreadable.append(exc)
                    _back_off(state["sessions"].setdefault(session_id, _new_session_state()), now)
                    changed = True
                    continue
                if not runs:
                    # A conversation with no Matrix turns, such as one through the OpenAI-compatible API, has nothing to
                    # ingest, so it does not take a place in the pass.
                    _finish(state["sessions"].setdefault(session_id, _new_session_state()), updated_at, 0)
                    changed = True
                    continue
                due.append(_DueSession(session_id, updated_at, runs))
            return due, changed, unreadable
        finally:
            storage.close()

    async def _ingest(
        self,
        config: Config,
        scope: GraphScope,
        state: _GraphState,
        session: _DueSession,
        automation_thread_ids: set[str],
        names: EntityNames,
        now: float,
    ) -> bool | None:
        """Ingest a bounded number of one conversation's new episodes and return whether none failed or was dropped.

        Return None when nothing was added because its agent cannot say yet what was redacted, the check failed, or the
        first episode failed and waits for its retry, which leaves the conversation for a later pass.
        """
        settings = config.knowledge_graph.ingest
        finished, blocked_until = finished_runs(session.runs, config, now=now)
        try:
            finished = await unredacted_runs(finished, scope.agent_name, self.redacted_event_ids)
        except Exception as exc:
            # One conversation whose redactions cannot be checked must not stop the agent's others.
            self._hold_failure(scope, "Knowledge graph ingestion could not check a conversation for redactions", exc)
            _back_off(state["sessions"].setdefault(session.session_id, _new_session_state()), now)
            await self._persist(scope.graph_name, state)
            return None
        if finished is None:
            return None
        entry = state["sessions"].setdefault(session.session_id, _new_session_state())
        # A conversation nothing was ingested from yet starts after the backfill window, however it was first recorded.
        if not entry["markers"]:
            _start_after_backfill_window(
                entry,
                finished,
                cutoff=state["started_at"] - settings.backfill_days * _DAY_SECONDS,
            )
        thread_kind = classify_thread(session.runs, automation_thread_ids)
        # From every finished turn, not only the new ones, so a person keeps their name in a turn stored without it.
        display_names = await asyncio.to_thread(newest_display_names, finished)
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
                names=names,
                display_names=display_names,
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
                    return False if added else None
                clean = clean and outcome
                added += 1
                # A long backlog spreads over passes; the conversation stays due because its update time is unchanged.
                if added >= _MAX_EPISODES_PER_PASS:
                    return clean
            if new_runs:
                entry["markers"][kind] = [*run_key(new_runs[-1]), RUN_DONE]
        # Held back by a turn waiting for approval, it is looked at again when those approvals expire, even without news.
        _finish(entry, session.updated_at, blocked_until or 0)
        await self._persist(scope.graph_name, state)
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
        piece = [kind, *entry["markers"].get(kind, [])]
        try:
            await self.store.add_episode(scope, rendered.episode, usage)
            dropped = False
        except Exception as exc:
            if entry["failing_piece"] != piece:
                # The counts belong to one episode, so another kind's successes never reset them.
                entry.update(failures=0, bad_outputs=0, failing_since=0, failing_piece=piece)
            # Only output the model got wrong counts here; a setup error such as a bad embedder waits like an outage.
            entry["bad_outputs"] = entry["bad_outputs"] + 1 if isinstance(exc, UnusableExtractionError) else 0
            entry["failing_since"] = entry["failing_since"] or now
            self._hold_failure(scope, "Knowledge graph ingestion failed", exc)
            if entry["bad_outputs"] < _MAX_BAD_OUTPUTS and now - entry["failing_since"] < _MAX_FAILING_SECONDS:
                _back_off(entry, now)
                await self._persist(scope.graph_name, state)
                return None
            logger.exception(
                "Knowledge graph dropped an episode it could not extract",
                agent=scope.agent_name,
                episode=rendered.episode.name,
            )
            dropped = True
        # Every piece records its progress, added or dropped, so a retry never repeats it.
        if entry["failing_piece"] == piece:
            entry.update(failures=0, bad_outputs=0, failing_since=0, failing_piece=[])
        entry["markers"][kind] = list(rendered.progress)
        await self._persist(scope.graph_name, state)
        return not dropped

    async def _persist(self, graph_name: str, state: _GraphState) -> None:
        await asyncio.to_thread(write_json_file_durable, self._state_path(graph_name), state)

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
        write_json_file_durable(self._state_path(graph_name), state)
        return state
