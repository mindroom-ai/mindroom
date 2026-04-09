"""Conversation-state persistence and cache-write helpers for bot flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio
from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom import constants
from mindroom.agents import (
    create_session_storage,
    get_agent_session,
    get_team_session,
)
from mindroom.handled_turns import HandledTurnState
from mindroom.history.runtime import create_scope_session_storage
from mindroom.history.types import HistoryScope
from mindroom.matrix.event_cache import normalize_event_source_for_cache
from mindroom.thread_utils import create_session_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import structlog
    from agno.db.sqlite import SqliteDb

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.event_cache import EventCache
    from mindroom.matrix.event_info import EventInfo
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass(frozen=True)
class PersistedTurnMetadata:
    """Run metadata needed to rebuild a coalesced turn after a partial ledger write."""

    anchor_event_id: str
    source_event_ids: tuple[str, ...]
    response_event_id: str | None = None
    source_event_prompts: dict[str, str] | None = None

    @property
    def is_coalesced(self) -> bool:
        """Return whether this persisted turn represents a coalesced batch."""
        return len(self.source_event_ids) > 1


@dataclass(frozen=True)
class LoadPersistedTurnMetadataRequest:
    """Inputs needed to recover persisted turn metadata for an edited message."""

    room: nio.MatrixRoom
    thread_id: str | None
    original_event_id: str
    requester_user_id: str


@dataclass(frozen=True)
class RemoveStaleRunsRequest:
    """Inputs needed to delete stale persisted runs for an edited message."""

    room: nio.MatrixRoom
    thread_id: str | None
    original_event_id: str
    requester_user_id: str


@dataclass(frozen=True)
class ConversationStateWriterDeps:
    """Static collaborators for conversation-state persistence and cache writes."""

    config_getter: Callable[[], Config]
    runtime_paths: RuntimePaths
    agent_name: str
    logger_getter: Callable[[], structlog.stdlib.BoundLogger]
    event_cache_getter: Callable[[], EventCache | None]
    fetch_thread_history: Callable[[nio.AsyncClient, str, str], Awaitable[Sequence[ResolvedVisibleMessage]]]


def _collect_sync_timeline_cache_updates(
    response: nio.SyncResponse,
) -> tuple[list[tuple[str, str, dict[str, Any]]], list[tuple[str, str]]]:
    """Extract cacheable timeline events and redactions from one sync response."""
    cached_events: list[tuple[str, str, dict[str, Any]]] = []
    redacted_events: list[tuple[str, str]] = []
    redacted_event_ids_by_room: dict[str, set[str]] = {}

    for room_id, room_info in response.rooms.join.items():
        _collect_room_sync_timeline_cache_updates(
            room_id=room_id,
            room_info=room_info,
            cached_events=cached_events,
            redacted_events=redacted_events,
            redacted_event_ids_by_room=redacted_event_ids_by_room,
        )

    filtered_cached_events = [
        (event_id, room_id, event_source)
        for event_id, room_id, event_source in cached_events
        if event_id not in redacted_event_ids_by_room.get(room_id, set())
    ]
    return filtered_cached_events, redacted_events


def _collect_room_sync_timeline_cache_updates(
    *,
    room_id: str,
    room_info: nio.RoomInfo,
    cached_events: list[tuple[str, str, dict[str, Any]]],
    redacted_events: list[tuple[str, str]],
    redacted_event_ids_by_room: dict[str, set[str]],
) -> None:
    """Collect cache writes for one joined room timeline."""
    for event in room_info.timeline.events:
        if not isinstance(event, nio.Event):
            continue
        if not isinstance(event.source, dict):
            continue
        if not isinstance(event.event_id, str):
            continue
        if isinstance(event, nio.RedactionEvent):
            if not isinstance(event.redacts, str):
                continue
            redacted_events.append((room_id, event.redacts))
            redacted_event_ids_by_room.setdefault(room_id, set()).add(event.redacts)
            continue

        server_timestamp = event.server_timestamp
        cached_events.append(
            (
                event.event_id,
                room_id,
                normalize_event_source_for_cache(
                    event.source,
                    event_id=event.event_id,
                    sender=event.sender if isinstance(event.sender, str) else None,
                    origin_server_ts=server_timestamp
                    if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
                    else None,
                ),
            ),
        )


@dataclass
class ConversationStateWriter:
    """Own the persisted conversation state and advisory cache writes for one bot."""

    deps: ConversationStateWriterDeps

    def _config(self) -> Config:
        """Return the bot's current live config."""
        return self.deps.config_getter()

    def _logger(self) -> structlog.stdlib.BoundLogger:
        """Return the bot's current live logger."""
        return self.deps.logger_getter()

    def _event_cache(self) -> EventCache | None:
        """Return the advisory event cache when enabled."""
        return self.deps.event_cache_getter()

    def history_scope(self) -> HistoryScope:
        """Return the persisted history scope backing this bot's runs."""
        if self.deps.agent_name in self._config().teams:
            return HistoryScope(kind="team", scope_id=self.deps.agent_name)
        return HistoryScope(kind="agent", scope_id=self.deps.agent_name)

    def history_session_type(self) -> SessionType:
        """Return the Agno session type used by this bot's persisted history."""
        return SessionType.TEAM if self.deps.agent_name in self._config().teams else SessionType.AGENT

    def create_history_scope_storage(self, execution_identity: ToolExecutionIdentity | None) -> SqliteDb:
        """Create the canonical storage backing this bot's persisted history scope."""
        config = self._config()
        if self.deps.agent_name not in config.teams:
            return create_session_storage(
                agent_name=self.deps.agent_name,
                config=config,
                runtime_paths=self.deps.runtime_paths,
                execution_identity=execution_identity,
            )
        return create_scope_session_storage(
            agent_name=self.deps.agent_name,
            scope=self.history_scope(),
            config=config,
            runtime_paths=self.deps.runtime_paths,
            execution_identity=execution_identity,
        )

    def team_history_scope(self, team_agents: list[MatrixID]) -> HistoryScope:
        """Return the persisted team-history scope for one team response."""
        config = self._config()
        if self.deps.agent_name in config.teams:
            return HistoryScope(kind="team", scope_id=self.deps.agent_name)
        team_member_names = [
            matrix_id.agent_name(config, self.deps.runtime_paths) or matrix_id.username for matrix_id in team_agents
        ]
        return HistoryScope(kind="team", scope_id=f"team_{'+'.join(sorted(team_member_names))}")

    def create_team_history_storage(
        self,
        *,
        team_agents: list[MatrixID],
        execution_identity: ToolExecutionIdentity | None,
        create_scope_session_storage_fn: Callable[..., SqliteDb] | None = None,
    ) -> SqliteDb:
        """Create the canonical shared storage backing one team response."""
        factory = (
            create_scope_session_storage if create_scope_session_storage_fn is None else create_scope_session_storage_fn
        )
        config = self._config()
        return factory(
            agent_name=self.deps.agent_name,
            scope=self.team_history_scope(team_agents),
            config=config,
            runtime_paths=self.deps.runtime_paths,
            execution_identity=execution_identity,
        )

    def persisted_turn_metadata_for_run(self, metadata: dict[str, Any]) -> PersistedTurnMetadata | None:
        """Parse persisted run metadata needed for coalesced edit regeneration."""
        anchor_event_id = metadata.get(constants.MATRIX_EVENT_ID_METADATA_KEY)
        if not isinstance(anchor_event_id, str) or not anchor_event_id:
            return None
        raw_source_event_ids = metadata.get(constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)
        raw_prompt_map = metadata.get(constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY)
        response_event_id = (
            metadata.get("matrix_response_event_id")
            if isinstance(metadata.get("matrix_response_event_id"), str)
            else None
        )
        handled_turn = HandledTurnState.create(
            raw_source_event_ids if isinstance(raw_source_event_ids, list) else [anchor_event_id],
            response_event_id=response_event_id,
            source_event_prompts=raw_prompt_map if isinstance(raw_prompt_map, dict) else None,
        )
        if not handled_turn.source_event_ids:
            handled_turn = HandledTurnState.from_source_event_id(
                anchor_event_id,
                response_event_id=response_event_id,
                source_event_prompts=raw_prompt_map if isinstance(raw_prompt_map, dict) else None,
            )
        return PersistedTurnMetadata(
            anchor_event_id=anchor_event_id,
            source_event_ids=handled_turn.source_event_ids,
            response_event_id=handled_turn.response_event_id,
            source_event_prompts=handled_turn.source_event_prompts,
        )

    def latest_matching_persisted_turn_metadata(
        self,
        runs: list[RunOutput | TeamRunOutput] | None,
        *,
        original_event_id: str,
    ) -> tuple[tuple[int | float, int], PersistedTurnMetadata] | None:
        """Return the newest persisted turn metadata in one session matching the edit target."""
        newest_match: tuple[tuple[int | float, int], PersistedTurnMetadata] | None = None
        for run_index, run in enumerate(runs or []):
            if not isinstance(run, (RunOutput, TeamRunOutput)):
                continue
            if not isinstance(run.metadata, dict):
                continue
            turn_metadata = self.persisted_turn_metadata_for_run(run.metadata)
            if turn_metadata is None:
                continue
            if (
                original_event_id != turn_metadata.anchor_event_id
                and original_event_id not in turn_metadata.source_event_ids
            ):
                continue
            run_created_at = run.created_at if isinstance(run.created_at, int | float) else 0
            sort_key = (run_created_at, run_index)
            if newest_match is None or sort_key > newest_match[0]:
                newest_match = (sort_key, turn_metadata)
        return newest_match

    def load_persisted_turn_metadata(
        self,
        request: LoadPersistedTurnMetadataRequest,
        *,
        build_message_target: Callable[..., MessageTarget],
        build_tool_execution_identity: Callable[..., ToolExecutionIdentity],
    ) -> PersistedTurnMetadata | None:
        """Load persisted run metadata for one edited turn when available."""
        session_type = self.history_session_type()
        session_contexts = [
            (request.thread_id, create_session_id(request.room.room_id, request.thread_id)),
            (None, create_session_id(request.room.room_id, None)),
        ]
        checked_session_ids: set[str] = set()
        newest_match: PersistedTurnMetadata | None = None
        newest_sort_key: tuple[int | float, int] | None = None
        for candidate_thread_id, session_id in session_contexts:
            if session_id in checked_session_ids:
                continue
            checked_session_ids.add(session_id)
            candidate_target = build_message_target(
                room_id=request.room.room_id,
                thread_id=candidate_thread_id,
                reply_to_event_id=request.original_event_id,
            )
            if candidate_thread_id is None:
                candidate_target = candidate_target.with_thread_root(None)
            execution_identity = build_tool_execution_identity(
                target=candidate_target,
                user_id=request.requester_user_id,
                session_id=session_id,
            )
            storage = self.create_history_scope_storage(execution_identity)
            try:
                session = (
                    get_team_session(storage, session_id)
                    if session_type is SessionType.TEAM
                    else get_agent_session(storage, session_id)
                )
                if session is None:
                    continue
                session_match = self.latest_matching_persisted_turn_metadata(
                    session.runs,
                    original_event_id=request.original_event_id,
                )
                if session_match is not None:
                    session_sort_key, turn_metadata = session_match
                    if newest_sort_key is None or session_sort_key > newest_sort_key:
                        newest_sort_key = session_sort_key
                        newest_match = turn_metadata
            finally:
                storage.close()
        return newest_match

    def persist_response_event_id_in_session_run(
        self,
        *,
        storage: SqliteDb,
        session_id: str,
        session_type: SessionType,
        run_id: str,
        response_event_id: str,
    ) -> None:
        """Persist Matrix response linkage onto the run that produced it."""
        session = (
            get_team_session(storage, session_id)
            if session_type is SessionType.TEAM
            else get_agent_session(storage, session_id)
        )
        if session is None or not session.runs:
            return
        for run in session.runs:
            if not isinstance(run, (RunOutput, TeamRunOutput)) or run.run_id != run_id:
                continue
            metadata = dict(run.metadata or {})
            if metadata.get("matrix_response_event_id") == response_event_id:
                return
            metadata["matrix_response_event_id"] = response_event_id
            run.metadata = metadata
            storage.upsert_session(session)
            return

    def remove_stale_runs_for_edited_message(
        self,
        request: RemoveStaleRunsRequest,
        *,
        build_message_target: Callable[..., MessageTarget],
        build_tool_execution_identity: Callable[..., ToolExecutionIdentity],
        remove_run_by_event_id_fn: Callable[..., bool],
    ) -> None:
        """Remove persisted runs tied to the pre-edit message before regenerating."""
        session_type = self.history_session_type()
        session_contexts = [
            (request.thread_id, create_session_id(request.room.room_id, request.thread_id)),
            (None, create_session_id(request.room.room_id, None)),
        ]
        checked_session_ids: set[str] = set()
        for candidate_thread_id, session_id in session_contexts:
            if session_id in checked_session_ids:
                continue
            checked_session_ids.add(session_id)
            candidate_target = build_message_target(
                room_id=request.room.room_id,
                thread_id=candidate_thread_id,
                reply_to_event_id=request.original_event_id,
            )
            if candidate_thread_id is None:
                candidate_target = candidate_target.with_thread_root(None)
            execution_identity = build_tool_execution_identity(
                target=candidate_target,
                user_id=request.requester_user_id,
                session_id=session_id,
            )
            storage = self.create_history_scope_storage(execution_identity)
            try:
                removed = remove_run_by_event_id_fn(
                    storage,
                    session_id,
                    request.original_event_id,
                    session_type=session_type,
                )
            finally:
                storage.close()
            if removed:
                self._logger().info(
                    "Removed stale run for edited message",
                    event_id=request.original_event_id,
                    session_id=session_id,
                )

    async def fetch_thread_history(
        self,
        client: nio.AsyncClient,
        room_id: str,
        thread_id: str,
    ) -> list[ResolvedVisibleMessage]:
        """Fetch thread history through the writer-owned cache maintenance API."""
        return list(await self.deps.fetch_thread_history(client, room_id, thread_id))

    async def cache_thread_event(
        self,
        room_id: str,
        event: nio.RoomMessageText,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append a live thread event into the advisory cache when the thread is known."""
        event_cache = self._event_cache()
        if event_cache is None:
            return

        thread_id = event_info.thread_id
        if thread_id is None and event_info.is_edit and event_info.original_event_id is not None:
            thread_id = event_info.thread_id_from_edit
            if thread_id is None:
                try:
                    thread_id = await event_cache.get_thread_id_for_event(
                        room_id,
                        event_info.original_event_id,
                    )
                except Exception as exc:
                    self._logger().warning(
                        "Failed to resolve cached thread for live edit event",
                        room_id=room_id,
                        event_id=event.event_id,
                        original_event_id=event_info.original_event_id,
                        error=str(exc),
                    )
                    return
        if thread_id is None:
            return

        server_timestamp = event.server_timestamp
        event_source = normalize_event_source_for_cache(
            event.source,
            event_id=event.event_id,
            sender=event.sender,
            origin_server_ts=server_timestamp
            if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
            else None,
        )

        try:
            await event_cache.append_event(room_id, thread_id, event_source)
        except Exception as exc:
            self._logger().warning(
                "Failed to append live thread event to cache",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event.event_id,
                error=str(exc),
            )

    async def cache_redaction_event(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        event_cache = self._event_cache()
        if event_cache is None:
            return

        try:
            thread_id = await event_cache.get_thread_id_for_event(room_id, event.redacts)
        except Exception as exc:
            self._logger().warning(
                "Failed to resolve cached thread for redaction",
                room_id=room_id,
                event_id=event.event_id,
                redacted_event_id=event.redacts,
                error=str(exc),
            )
            thread_id = None
        server_timestamp = event.server_timestamp
        redaction_source = normalize_event_source_for_cache(
            event.source,
            event_id=event.event_id,
            sender=event.sender,
            origin_server_ts=server_timestamp
            if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
            else None,
        )

        try:
            await event_cache.redact_event(
                room_id,
                event.redacts,
                thread_id=thread_id,
                redaction_event=redaction_source,
            )
        except Exception as exc:
            self._logger().warning(
                "Failed to apply live redaction to cache",
                room_id=room_id,
                thread_id=thread_id,
                redacted_event_id=event.redacts,
                error=str(exc),
            )

    async def cache_sync_timeline_events(self, response: nio.SyncResponse) -> None:
        """Persist sync timeline events so later thread lookups can use the advisory cache."""
        event_cache = self._event_cache()
        if event_cache is None:
            return

        filtered_cached_events, redacted_events = _collect_sync_timeline_cache_updates(response)
        if not filtered_cached_events and not redacted_events:
            return

        try:
            if filtered_cached_events:
                await event_cache.store_events_batch(filtered_cached_events)
            for room_id, redacted_event_id in redacted_events:
                await event_cache.redact_event(room_id, redacted_event_id)
        except Exception as exc:
            self._logger().warning(
                "Failed to cache sync timeline events",
                error=str(exc),
                events=len(filtered_cached_events),
                redactions=len(redacted_events),
            )
