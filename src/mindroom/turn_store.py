"""Unified durable turn ownership for runtime flows."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom.agent_storage import get_agent_session, get_team_session
from mindroom.agents import remove_run_by_event_id
from mindroom.handled_turns import HandledTurnLedger, TurnRecord, TurnRecordCodec
from mindroom.session_ids import create_session_id

if TYPE_CHECKING:
    import nio

    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.history.types import HistoryScope
    from mindroom.message_target import MessageTarget
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.turn_policy import ResponseAction


@dataclass(frozen=True)
class _LoadPersistedTurnRequest:
    """Inputs needed to recover one turn from Agno run metadata."""

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
    """Own replication, precedence, backfill, and repair for one entity's turns.

    The handled-turn ledger is authoritative whenever a row exists.
    Agno run metadata recovers a missing ledger row and backfills only optional
    facts that are absent from an existing row.
    Any recovered or enriched record is repaired back into the ledger before it
    is returned to the caller.
    """

    deps: TurnStoreDeps
    _ledger: HandledTurnLedger = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Construct the private handled-turn ledger for this runtime entity."""
        self._ledger = HandledTurnLedger(
            self.deps.agent_name,
            base_path=Path(self.deps.tracking_base_path),
        )

    def warm(self) -> None:
        """Load the durable ledger from disk; call from a worker thread, not the event loop."""
        self._ledger.warm()

    def record_turn(self, turn_record: TurnRecord) -> None:
        """Persist one terminal turn, preserving any previously recorded optional facts."""
        if not turn_record.source_event_ids:
            return
        existing_record = self._existing_record_for_sources(turn_record.source_event_ids)
        merged_record = (
            _backfill_missing_turn_facts(turn_record, existing_record) if existing_record is not None else turn_record
        )
        visible_echo_event_id = merged_record.visible_echo_event_id or self.visible_echo_for_sources(
            merged_record.source_event_ids,
        )
        self._ledger.record_handled_turn(
            replace(
                merged_record,
                completed=True,
                visible_echo_event_id=visible_echo_event_id,
            ),
        )

    def is_handled(self, event_id: str) -> bool:
        """Return whether one source event already has a terminal outcome."""
        return self._ledger.has_responded(event_id)

    def visible_echo_for_source(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo for one source event."""
        return self._ledger.get_visible_echo_event_id(source_event_id)

    def record_visible_echo(self, source_event_id: str, echo_event_id: str) -> None:
        """Track a visible echo without changing an existing completion outcome."""
        turn_record = self._ledger.get_turn_record(source_event_id)
        if turn_record is None:
            turn_record = TurnRecord.create([source_event_id], completed=False)
        self._ledger.record_handled_turn(replace(turn_record, visible_echo_event_id=echo_event_id))

    def visible_echo_for_sources(self, source_event_ids: tuple[str, ...]) -> str | None:
        """Return the first visible echo already tracked for one or more source events."""
        return self._ledger.visible_echo_event_id_for_sources(source_event_ids)

    def get_turn_record(self, source_event_id: str) -> TurnRecord | None:
        """Return the ledger-backed canonical record for one source event."""
        return self._ledger.get_turn_record(source_event_id)

    def response_history_scope(
        self,
        response_action: ResponseAction,
        *,
        requester_user_id: str | None = None,
    ) -> HistoryScope:
        """Return the persisted history scope used by one response action."""
        if response_action.kind == "individual":
            return self.deps.state_writer.history_scope()
        if response_action.kind == "team":
            assert response_action.form_team is not None
            return self.deps.state_writer.team_history_scope(
                response_action.form_team.eligible_members,
                requester_user_id=requester_user_id,
            )
        msg = f"Response history scope is not defined for {response_action.kind!r} actions"
        raise ValueError(msg)

    def attach_response_context(
        self,
        turn_record: TurnRecord,
        *,
        history_scope: HistoryScope | None,
        conversation_target: MessageTarget,
    ) -> TurnRecord:
        """Attach the persisted regeneration context for one response."""
        return replace(
            turn_record,
            response_owner=self.deps.agent_name,
            history_scope=history_scope,
            conversation_target=conversation_target,
        )

    def build_run_metadata(
        self,
        turn_record: TurnRecord,
        *,
        additional_source_event_ids: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Project one record into versioned recoverable Agno run metadata.

        ``additional_source_event_ids`` lets one anchored run stay discoverable by
        extra triggering events, such as a numeric interactive reply whose response
        still anchors to the original question event.
        """
        projected_record = turn_record
        if additional_source_event_ids:
            base_source_event_ids = turn_record.source_event_ids if turn_record.is_coalesced else ()
            normalized_source_event_ids = TurnRecord.create(
                (*base_source_event_ids, *additional_source_event_ids),
            ).source_event_ids
            projected_record = replace(turn_record, source_event_ids=normalized_source_event_ids)
        metadata = TurnRecordCodec.to_run_metadata(projected_record)
        return dict(metadata) if metadata else None

    def load_turn(
        self,
        *,
        room: nio.MatrixRoom,
        thread_id: str | None,
        original_event_id: str,
        requester_user_id: str,
    ) -> TurnRecord | None:
        """Load, deterministically merge, and repair one durable turn record."""
        ledger_record = self._ledger.get_turn_record(original_event_id)
        recovery_record = self._load_persisted_turn_record(
            _LoadPersistedTurnRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
        )
        if ledger_record is None and recovery_record is None:
            return None
        if ledger_record is None:
            assert recovery_record is not None
            merged_record = recovery_record
        elif recovery_record is None:
            merged_record = ledger_record
        else:
            merged_record = _backfill_missing_turn_facts(ledger_record, recovery_record)
        if merged_record != ledger_record:
            self._ledger.record_handled_turn(merged_record)
        return merged_record

    def remove_stale_runs_for_edit(
        self,
        *,
        turn_record: TurnRecord,
        requester_user_id: str,
    ) -> None:
        """Remove stale persisted runs before regenerating one edited turn."""
        self._remove_stale_runs_for_turn_record(
            turn_record=turn_record,
            requester_user_id=requester_user_id,
        )

    def _latest_matching_persisted_turn_record(
        self,
        runs: list[RunOutput | TeamRunOutput] | None,
        *,
        original_event_id: str,
    ) -> tuple[tuple[int | float, int], TurnRecord] | None:
        """Return the newest persisted turn record in one session matching the edit target."""
        newest_match: tuple[tuple[int | float, int], TurnRecord] | None = None
        for run_index, run in enumerate(runs or []):
            if not isinstance(run, (RunOutput, TeamRunOutput)):
                continue
            if not isinstance(run.metadata, dict):
                continue
            turn_record = TurnRecordCodec.from_run_metadata(run.metadata)
            if turn_record is None:
                continue
            if (
                original_event_id != turn_record.anchor_event_id
                and original_event_id not in turn_record.source_event_ids
            ):
                continue
            run_created_at = run.created_at if isinstance(run.created_at, int | float) else 0
            sort_key = (run_created_at, run_index)
            if newest_match is None or sort_key > newest_match[0]:
                newest_match = (sort_key, turn_record)
        return newest_match

    def _load_persisted_turn_record(
        self,
        request: _LoadPersistedTurnRequest,
    ) -> TurnRecord | None:
        """Load the newest matching recovery record across thread and room sessions."""
        history_scope = self.deps.state_writer.history_scope()
        session_type = self.deps.state_writer.session_type_for_scope(history_scope)
        session_contexts = [
            (request.thread_id, create_session_id(request.room.room_id, request.thread_id)),
            (None, create_session_id(request.room.room_id, None)),
        ]
        checked_session_ids: set[str] = set()
        newest_match: TurnRecord | None = None
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
                session_match = self._latest_matching_persisted_turn_record(
                    session.runs,
                    original_event_id=request.original_event_id,
                )
                if session_match is not None:
                    session_sort_key, turn_record = session_match
                    if newest_sort_key is None or session_sort_key > newest_sort_key:
                        newest_sort_key = session_sort_key
                        newest_match = turn_record
            finally:
                storage.close()
        return newest_match

    def _remove_stale_runs_for_turn_record(
        self,
        *,
        turn_record: TurnRecord,
        requester_user_id: str,
    ) -> bool:
        """Remove persisted runs using the exact recorded target and history scope."""
        if turn_record.conversation_target is None or turn_record.history_scope is None:
            return False
        session_id = turn_record.conversation_target.session_id
        execution_identity = self.deps.tool_runtime.build_execution_identity(
            target=turn_record.conversation_target,
            user_id=requester_user_id,
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

    def _existing_record_for_sources(self, source_event_ids: tuple[str, ...]) -> TurnRecord | None:
        """Return the first existing ledger record for one canonical source identity."""
        for source_event_id in source_event_ids:
            if turn_record := self._ledger.get_turn_record(source_event_id):
                return turn_record
        return None


def _backfill_missing_turn_facts(authority: TurnRecord, recovery: TurnRecord) -> TurnRecord:
    """Fill absent optional facts without overriding authoritative ledger values.

    Source identity, anchor, completion, and timestamp always come from
    ``authority``. Every optional fact uses ``recovery`` only when the
    authoritative value is absent.
    """
    return replace(
        authority,
        response_event_id=authority.response_event_id or recovery.response_event_id,
        visible_echo_event_id=authority.visible_echo_event_id or recovery.visible_echo_event_id,
        source_event_prompts=(
            authority.source_event_prompts
            if authority.source_event_prompts is not None
            else recovery.source_event_prompts
        ),
        source_event_metadata=(
            authority.source_event_metadata
            if authority.source_event_metadata is not None
            else recovery.source_event_metadata
        ),
        response_owner=authority.response_owner or recovery.response_owner,
        requester_id=authority.requester_id or recovery.requester_id,
        correlation_id=authority.correlation_id or recovery.correlation_id,
        history_scope=authority.history_scope or recovery.history_scope,
        conversation_target=authority.conversation_target or recovery.conversation_target,
    )
