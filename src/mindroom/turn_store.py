"""Unified durable turn access for runtime flows."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom import constants
from mindroom.agent_storage import get_agent_session, get_team_session
from mindroom.agents import remove_run_by_event_id
from mindroom.handled_turns import HandledTurnLedger, HandledTurnRecord, HandledTurnState
from mindroom.thread_utils import create_session_id

if TYPE_CHECKING:
    import nio

    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.history import HistoryScope
    from mindroom.message_target import MessageTarget
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.turn_policy import ResponseAction


@dataclass(frozen=True)
class LoadedTurnRecord:
    """Merged durable turn state used by regeneration and dispatch flows."""

    record: HandledTurnRecord
    recorded_turn_context_available: bool
    response_owner_missing: bool
    requires_backfill: bool


@dataclass(frozen=True)
class _PersistedTurnMetadata:
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
class _LoadPersistedTurnMetadataRequest:
    """Inputs needed to recover persisted turn metadata for an edited message."""

    room: nio.MatrixRoom
    thread_id: str | None
    original_event_id: str
    requester_user_id: str


@dataclass(frozen=True)
class _RemoveStaleRunsRequest:
    """Inputs needed to delete stale persisted runs for an edited message."""

    room: nio.MatrixRoom
    thread_id: str | None
    original_event_id: str
    requester_user_id: str


@dataclass(frozen=True)
class TurnStoreDeps:
    """Collaborators needed to read and write durable turn state."""

    agent_name: str
    tracking_base_path: Path | str
    state_writer: ConversationStateWriter
    resolver: ConversationResolver
    tool_runtime: ToolRuntimeSupport


@dataclass
class TurnStore:
    """Own the runtime-facing durable turn record for one entity."""

    deps: TurnStoreDeps
    _ledger: HandledTurnLedger = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Construct the private handled-turn ledger for this runtime entity."""
        self._ledger = HandledTurnLedger(
            self.deps.agent_name,
            base_path=Path(self.deps.tracking_base_path),
        )

    def record_turn(self, handled_turn: HandledTurnState) -> None:
        """Persist one terminal handled-turn outcome."""
        visible_echo_event_id = handled_turn.visible_echo_event_id or self.visible_echo_for_sources(
            handled_turn.source_event_ids,
        )
        self._ledger.record_handled_turn(
            handled_turn.with_visible_echo_event_id(visible_echo_event_id),
        )

    def record_turn_record(self, turn_record: HandledTurnRecord) -> None:
        """Persist one exact handled-turn record without losing its anchor event."""
        self._ledger.record_handled_turn_record(turn_record)

    def is_handled(self, event_id: str) -> bool:
        """Return whether one source event already has a terminal outcome."""
        return self._ledger.has_responded(event_id)

    def visible_echo_for_source(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo for one source event."""
        return self._ledger.get_visible_echo_event_id(source_event_id)

    def record_visible_echo(self, source_event_id: str, echo_event_id: str) -> None:
        """Track a visible echo before the turn reaches a terminal outcome."""
        self._ledger.record_visible_echo(source_event_id, echo_event_id)

    def visible_echo_for_sources(self, source_event_ids: tuple[str, ...]) -> str | None:
        """Return the first visible echo already tracked for one or more source events."""
        return self._ledger.visible_echo_event_id_for_sources(source_event_ids)

    def get_turn_record(self, source_event_id: str) -> HandledTurnRecord | None:
        """Return the ledger-backed turn record for one source event when available."""
        return self._ledger.get_turn_record(source_event_id)

    def response_history_scope(
        self,
        response_action: ResponseAction | None = None,
    ) -> HistoryScope | None:
        """Return the persisted history scope used by one response action."""
        if response_action is None or response_action.kind == "individual":
            return self.deps.state_writer.history_scope()
        if response_action.kind == "team":
            assert response_action.form_team is not None
            return self.deps.state_writer.team_history_scope(response_action.form_team.eligible_members)
        return None

    def attach_response_context(
        self,
        handled_turn: HandledTurnState,
        *,
        history_scope: HistoryScope | None,
        conversation_target: MessageTarget | None,
    ) -> HandledTurnState:
        """Attach the persisted regeneration context for one response."""
        return handled_turn.with_response_context(
            response_owner=self.deps.agent_name,
            history_scope=history_scope,
            conversation_target=conversation_target,
        )

    def build_run_metadata(
        self,
        handled_turn: HandledTurnState,
        *,
        additional_source_event_ids: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Return persisted run metadata for one handled turn.

        ``additional_source_event_ids`` lets one anchored run stay discoverable by
        extra triggering events, such as a numeric interactive reply whose response
        still anchors to the original question event.
        """
        metadata = self._build_run_metadata_for_handled_turn(handled_turn) or {}
        if additional_source_event_ids:
            source_event_ids = [
                event_id
                for event_id in metadata.get(constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, [])
                if isinstance(event_id, str) and event_id
            ]
            for event_id in additional_source_event_ids:
                if not event_id or event_id in source_event_ids:
                    continue
                source_event_ids.append(event_id)
            if source_event_ids:
                metadata[constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] = source_event_ids
        return metadata or None

    @staticmethod
    def _build_run_metadata_for_handled_turn(
        handled_turn: HandledTurnState,
    ) -> dict[str, Any] | None:
        """Build persisted run metadata for one handled turn."""
        if not handled_turn.is_coalesced:
            return None
        metadata: dict[str, Any] = {
            constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: list(handled_turn.source_event_ids),
        }
        if handled_turn.source_event_prompts:
            metadata[constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY] = dict(handled_turn.source_event_prompts)
        return metadata

    def load_turn(
        self,
        *,
        room: nio.MatrixRoom,
        thread_id: str | None,
        original_event_id: str,
        requester_user_id: str,
    ) -> LoadedTurnRecord | None:
        """Load one merged durable turn record for an edited or replayed source event."""
        turn_record = self._ledger.get_turn_record(original_event_id)
        ledger_turn_record = turn_record
        persisted_turn_metadata = self._load_persisted_turn_metadata(
            _LoadPersistedTurnMetadataRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
        )
        if turn_record is None and persisted_turn_metadata is None:
            return None
        if turn_record is None:
            assert persisted_turn_metadata is not None
            turn_record = HandledTurnRecord(
                anchor_event_id=persisted_turn_metadata.anchor_event_id,
                source_event_ids=persisted_turn_metadata.source_event_ids,
                response_event_id=persisted_turn_metadata.response_event_id,
                source_event_prompts=persisted_turn_metadata.source_event_prompts,
            )
        recorded_turn_context_available = bool(
            turn_record.conversation_target is not None and turn_record.history_scope is not None,
        )
        response_owner_missing = turn_record.response_owner is None
        if persisted_turn_metadata is None:
            return LoadedTurnRecord(
                record=turn_record,
                recorded_turn_context_available=recorded_turn_context_available,
                response_owner_missing=response_owner_missing,
                requires_backfill=False,
            )
        merged_prompt_map = turn_record.source_event_prompts
        if merged_prompt_map is None and persisted_turn_metadata.is_coalesced:
            merged_prompt_map = persisted_turn_metadata.source_event_prompts
        merged_turn_record = replace(
            turn_record,
            anchor_event_id=persisted_turn_metadata.anchor_event_id,
            response_event_id=persisted_turn_metadata.response_event_id or turn_record.response_event_id,
            source_event_prompts=merged_prompt_map,
        )
        return LoadedTurnRecord(
            record=merged_turn_record,
            recorded_turn_context_available=recorded_turn_context_available,
            response_owner_missing=response_owner_missing,
            requires_backfill=ledger_turn_record is None or merged_turn_record != ledger_turn_record,
        )

    def remove_stale_runs_for_edit(
        self,
        *,
        loaded_turn: LoadedTurnRecord,
        room: nio.MatrixRoom,
        thread_id: str | None,
        original_event_id: str,
        requester_user_id: str,
    ) -> None:
        """Remove stale persisted runs before regenerating one edited turn."""
        if (
            loaded_turn.recorded_turn_context_available
            and loaded_turn.record.conversation_target is not None
            and loaded_turn.record.history_scope is not None
        ):
            self._remove_stale_runs_for_turn_record(
                turn_record=loaded_turn.record,
                requester_user_id=requester_user_id,
            )
            return
        self._remove_stale_runs_for_edited_message(
            _RemoveStaleRunsRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
        )

    def _persisted_turn_metadata_for_run(self, metadata: dict[str, Any]) -> _PersistedTurnMetadata | None:
        """Parse persisted run metadata needed for coalesced edit regeneration."""
        anchor_event_id = metadata.get(constants.MATRIX_EVENT_ID_METADATA_KEY)
        if not isinstance(anchor_event_id, str) or not anchor_event_id:
            return None
        raw_source_event_ids = metadata.get(constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)
        raw_prompt_map = metadata.get(constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY)
        raw_response_event_id = metadata.get(constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY)
        response_event_id = raw_response_event_id if isinstance(raw_response_event_id, str) else None
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
        return _PersistedTurnMetadata(
            anchor_event_id=anchor_event_id,
            source_event_ids=handled_turn.source_event_ids,
            response_event_id=handled_turn.response_event_id,
            source_event_prompts=handled_turn.source_event_prompts,
        )

    def _latest_matching_persisted_turn_metadata(
        self,
        runs: list[RunOutput | TeamRunOutput] | None,
        *,
        original_event_id: str,
    ) -> tuple[tuple[int | float, int], _PersistedTurnMetadata] | None:
        """Return the newest persisted turn metadata in one session matching the edit target."""
        newest_match: tuple[tuple[int | float, int], _PersistedTurnMetadata] | None = None
        for run_index, run in enumerate(runs or []):
            if not isinstance(run, (RunOutput, TeamRunOutput)):
                continue
            if not isinstance(run.metadata, dict):
                continue
            turn_metadata = self._persisted_turn_metadata_for_run(run.metadata)
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

    def _load_persisted_turn_metadata(
        self,
        request: _LoadPersistedTurnMetadataRequest,
    ) -> _PersistedTurnMetadata | None:
        """Load persisted run metadata for one edited turn when available."""
        history_scope = self.deps.state_writer.history_scope()
        session_type = self.deps.state_writer.session_type_for_scope(history_scope)
        session_contexts = [
            (request.thread_id, create_session_id(request.room.room_id, request.thread_id)),
            (None, create_session_id(request.room.room_id, None)),
        ]
        checked_session_ids: set[str] = set()
        newest_match: _PersistedTurnMetadata | None = None
        newest_sort_key: tuple[int | float, int] | None = None
        for candidate_thread_id, session_id in session_contexts:
            if session_id in checked_session_ids:
                continue
            checked_session_ids.add(session_id)
            candidate_target = self.deps.resolver.build_message_target(
                room_id=request.room.room_id,
                thread_id=candidate_thread_id,
                reply_to_event_id=request.original_event_id,
            )
            if candidate_thread_id is None:
                candidate_target = candidate_target.with_thread_root(None)
            execution_identity = self.deps.tool_runtime.build_execution_identity(
                target=candidate_target,
                user_id=request.requester_user_id,
                session_id=session_id,
            )
            storage = self.deps.state_writer.create_storage(execution_identity, scope=history_scope)
            try:
                session = (
                    get_team_session(storage, session_id)
                    if session_type is SessionType.TEAM
                    else get_agent_session(storage, session_id)
                )
                if session is None:
                    continue
                session_match = self._latest_matching_persisted_turn_metadata(
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

    def _remove_stale_runs_for_edited_message(
        self,
        request: _RemoveStaleRunsRequest,
    ) -> None:
        """Remove persisted runs tied to the pre-edit message before regenerating."""
        history_scope = self.deps.state_writer.history_scope()
        session_type = self.deps.state_writer.session_type_for_scope(history_scope)
        session_contexts = [
            (request.thread_id, create_session_id(request.room.room_id, request.thread_id)),
            (None, create_session_id(request.room.room_id, None)),
        ]
        checked_session_ids: set[str] = set()
        for candidate_thread_id, session_id in session_contexts:
            if session_id in checked_session_ids:
                continue
            checked_session_ids.add(session_id)
            candidate_target = self.deps.resolver.build_message_target(
                room_id=request.room.room_id,
                thread_id=candidate_thread_id,
                reply_to_event_id=request.original_event_id,
            )
            if candidate_thread_id is None:
                candidate_target = candidate_target.with_thread_root(None)
            execution_identity = self.deps.tool_runtime.build_execution_identity(
                target=candidate_target,
                user_id=request.requester_user_id,
                session_id=session_id,
            )
            storage = self.deps.state_writer.create_storage(execution_identity, scope=history_scope)
            try:
                removed = remove_run_by_event_id(
                    storage,
                    session_id,
                    request.original_event_id,
                    session_type=session_type,
                )
            finally:
                storage.close()
            if removed:
                self.deps.state_writer.deps.logger.info(
                    "Removed stale run for edited message",
                    event_id=request.original_event_id,
                    session_id=session_id,
                )

    def _remove_stale_runs_for_turn_record(
        self,
        *,
        turn_record: HandledTurnRecord,
        requester_user_id: str,
    ) -> bool:
        """Remove persisted runs using the exact recorded target and history scope."""
        if turn_record.conversation_target is None or turn_record.history_scope is None:
            return False
        session_id = turn_record.conversation_target.session_id
        execution_identity = self.deps.tool_runtime.build_execution_identity(
            target=turn_record.conversation_target,
            user_id=requester_user_id,
            session_id=session_id,
        )
        storage = self.deps.state_writer.create_storage(
            execution_identity,
            scope=turn_record.history_scope,
        )
        removed_any = False
        try:
            session_type = self.deps.state_writer.session_type_for_scope(turn_record.history_scope)
            for source_event_id in turn_record.source_event_ids:
                removed_any = (
                    remove_run_by_event_id(
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
            self.deps.state_writer.deps.logger.info(
                "Removed stale run for edited handled turn",
                source_event_ids=list(turn_record.source_event_ids),
                session_id=session_id,
                history_scope=turn_record.history_scope.key,
            )
        return removed_any
