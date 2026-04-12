"""Conversation-state persistence helpers for bot flows."""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom import constants
from mindroom.agents import (
    create_session_storage,
    get_agent_session,
    get_team_session,
)
from mindroom.handled_turns import HandledTurnRecord, HandledTurnState
from mindroom.history.runtime import create_scope_session_storage
from mindroom.history.types import HistoryScope
from mindroom.thread_utils import create_session_id

if TYPE_CHECKING:
    import nio
    import structlog
    from agno.db.sqlite import SqliteDb

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
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

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str


@dataclass
class ConversationStateWriter:
    """Own the persisted conversation state for one bot."""

    deps: ConversationStateWriterDeps

    def history_scope(self) -> HistoryScope:
        """Return the persisted history scope backing this bot's runs."""
        if self.deps.agent_name in self.deps.runtime.config.teams:
            return HistoryScope(kind="team", scope_id=self.deps.agent_name)
        return HistoryScope(kind="agent", scope_id=self.deps.agent_name)

    def history_session_type(self) -> SessionType:
        """Return the Agno session type used by this bot's persisted history."""
        return SessionType.TEAM if self.deps.agent_name in self.deps.runtime.config.teams else SessionType.AGENT

    def create_history_scope_storage(self, execution_identity: ToolExecutionIdentity | None) -> SqliteDb:
        """Create the canonical storage backing this bot's persisted history scope."""
        config = self.deps.runtime.config
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

    def create_storage_for_history_scope(
        self,
        *,
        scope: HistoryScope,
        execution_identity: ToolExecutionIdentity | None,
    ) -> SqliteDb:
        """Create storage for one exact persisted history scope."""
        normalized_scope = HistoryScope(kind=scope.kind, scope_id=scope.scope_id)
        if normalized_scope == self.history_scope():
            return self.create_history_scope_storage(execution_identity)
        return create_scope_session_storage(
            agent_name=normalized_scope.scope_id if normalized_scope.kind == "agent" else self.deps.agent_name,
            scope=normalized_scope,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            execution_identity=execution_identity,
        )

    @staticmethod
    def session_type_for_history_scope(scope: HistoryScope) -> SessionType:
        """Return the Agno session type used by one persisted history scope."""
        return SessionType.TEAM if scope.kind == "team" else SessionType.AGENT

    def team_history_scope(self, team_agents: list[MatrixID]) -> HistoryScope:
        """Return the persisted team-history scope for one team response."""
        config = self.deps.runtime.config
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
        config = self.deps.runtime.config
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
                self.deps.logger.info(
                    "Removed stale run for edited message",
                    event_id=request.original_event_id,
                    session_id=session_id,
                )

    def remove_stale_runs_for_turn_record(
        self,
        *,
        turn_record: HandledTurnRecord,
        requester_user_id: str,
        build_tool_execution_identity: Callable[..., ToolExecutionIdentity],
        remove_run_by_event_id_fn: Callable[..., bool],
    ) -> bool:
        """Remove persisted runs using the exact recorded target and history scope."""
        if turn_record.conversation_target is None or turn_record.history_scope is None:
            return False
        session_id = turn_record.conversation_target.session_id
        execution_identity = build_tool_execution_identity(
            target=turn_record.conversation_target,
            user_id=requester_user_id,
            session_id=session_id,
        )
        storage = self.create_storage_for_history_scope(
            scope=turn_record.history_scope,
            execution_identity=execution_identity,
        )
        removed_any = False
        try:
            session_type = self.session_type_for_history_scope(turn_record.history_scope)
            for source_event_id in turn_record.source_event_ids:
                removed_any = (
                    remove_run_by_event_id_fn(
                        storage,
                        session_id,
                        source_event_id,
                        session_type=session_type,
                    )
                    or removed_any
                )
        finally:
            storage.close()
        if removed_any:
            self.deps.logger.info(
                "Removed stale run for edited handled turn",
                source_event_ids=list(turn_record.source_event_ids),
                session_id=session_id,
                history_scope=turn_record.history_scope.key,
            )
        return removed_any
