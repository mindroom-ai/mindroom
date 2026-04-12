"""Unified durable turn access for runtime flows."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom import constants
from mindroom.agents import remove_run_by_event_id
from mindroom.conversation_state_writer import (
    LoadPersistedTurnMetadataRequest,
    RemoveStaleRunsRequest,
)
from mindroom.handled_turns import HandledTurnLedger, HandledTurnRecord, HandledTurnState

if TYPE_CHECKING:
    import nio

    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.history.types import HistoryScope
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport


@dataclass(frozen=True)
class LoadedTurnRecord:
    """Merged durable turn state used by regeneration and dispatch flows."""

    record: HandledTurnRecord
    recorded_turn_context_available: bool
    response_owner_missing: bool
    requires_backfill: bool


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

    def mark_handled(self, handled_turn: HandledTurnState) -> None:
        """Persist one terminal handled-turn outcome."""
        visible_echo_event_id = handled_turn.visible_echo_event_id or self.visible_echo_event_id_for_sources(
            handled_turn.source_event_ids,
        )
        self._ledger.record_handled_turn(
            handled_turn.with_visible_echo_event_id(visible_echo_event_id),
        )

    def has_responded(self, event_id: str) -> bool:
        """Return whether one source event already has a terminal outcome."""
        return self._ledger.has_responded(event_id)

    def get_visible_echo_event_id(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo for one source event."""
        return self._ledger.get_visible_echo_event_id(source_event_id)

    def record_visible_echo(self, source_event_id: str, echo_event_id: str) -> None:
        """Track a visible echo before the turn reaches a terminal outcome."""
        self._ledger.record_visible_echo(source_event_id, echo_event_id)

    def visible_echo_event_id_for_sources(self, source_event_ids: tuple[str, ...]) -> str | None:
        """Return the first visible echo already tracked for one or more source events."""
        return self._ledger.visible_echo_event_id_for_sources(source_event_ids)

    def get_turn_record(self, source_event_id: str) -> HandledTurnRecord | None:
        """Return the ledger-backed turn record for one source event when available."""
        return self._ledger.get_turn_record(source_event_id)

    def default_history_scope(self) -> HistoryScope:
        """Return the default persisted history scope for this runtime entity."""
        return self.deps.state_writer.history_scope()

    def matrix_run_metadata(
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
        metadata = self._matrix_run_metadata_for_handled_turn(handled_turn) or {}
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
    def _matrix_run_metadata_for_handled_turn(
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

    def load_turn_record(
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
        persisted_turn_metadata = self.deps.state_writer.load_persisted_turn_metadata(
            LoadPersistedTurnMetadataRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
            build_message_target=self.deps.resolver.build_message_target,
            build_tool_execution_identity=self.deps.tool_runtime.build_execution_identity,
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
            self.deps.state_writer.remove_stale_runs_for_turn_record(
                turn_record=loaded_turn.record,
                requester_user_id=requester_user_id,
                build_tool_execution_identity=self.deps.tool_runtime.build_execution_identity,
                remove_run_by_event_id_fn=remove_run_by_event_id,
            )
            return
        self.deps.state_writer.remove_stale_runs_for_edited_message(
            RemoveStaleRunsRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
            build_message_target=self.deps.resolver.build_message_target,
            build_tool_execution_identity=self.deps.tool_runtime.build_execution_identity,
            remove_run_by_event_id_fn=remove_run_by_event_id,
        )
