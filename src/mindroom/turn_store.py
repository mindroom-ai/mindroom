"""Unified durable turn ownership for runtime flows."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom.agent_storage import get_agent_session, get_team_session
from mindroom.agents import remove_run_by_event_id
from mindroom.handled_turns import HandledTurnLedger, TurnRecord, TurnRecordCodec, same_turn_identity
from mindroom.history.storage import invalidate_compacted_replay, read_scope_seen_event_ids
from mindroom.session_ids import create_session_id

if TYPE_CHECKING:
    from collections.abc import Mapping

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

    A present handled-turn ledger row owns canonical source identity and anchor.
    Newer delivered Agno run metadata repairs mutable response and regeneration
    facts; older or incomplete runs only backfill absent optional facts.
    Recovery never replaces a ledger record changed while metadata was loading.
    Any recovered or enriched record is repaired back into the ledger before it
    is returned to the caller.
    """

    deps: TurnStoreDeps
    _ledger: HandledTurnLedger = field(init=False, repr=False)
    _pending_claim_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _pending_claimed_event_ids: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        """Construct the private handled-turn ledger for this runtime entity."""
        self._ledger = HandledTurnLedger(
            self.deps.agent_name,
            base_path=Path(self.deps.tracking_base_path),
        )

    def warm(self) -> None:
        """Load the ledger before asynchronous startup recovery begins."""
        self._ledger.warm()

    def record_turn(self, turn_record: TurnRecord) -> None:
        """Persist one terminal turn, preserving any previously recorded optional facts."""
        if not turn_record.source_event_ids:
            return

        def terminal_record(existing_records: Mapping[str, TurnRecord]) -> TurnRecord:
            compatible_existing_records = tuple(
                existing
                for existing in existing_records.values()
                if not existing.completed or same_turn_identity(existing, turn_record)
            )
            existing_record = next(iter(compatible_existing_records), None)
            merged_record = (
                _backfill_missing_turn_facts(turn_record, existing_record)
                if existing_record is not None
                else turn_record
            )
            redacted_source_event_ids, pending_redaction_cleanup_event_ids = _merged_redaction_markers(
                turn_record,
                merged_record,
                compatible_existing_records,
            )
            visible_echo_event_id = merged_record.visible_echo_event_id or next(
                (
                    existing.visible_echo_event_id
                    for existing in compatible_existing_records
                    if existing.visible_echo_event_id is not None
                ),
                None,
            )
            return replace(
                merged_record,
                completed=True,
                redacted_source_event_ids=redacted_source_event_ids,
                pending_redaction_cleanup_event_ids=pending_redaction_cleanup_event_ids,
                visible_echo_event_id=visible_echo_event_id,
                timestamp=0.0,
            )

        self._ledger.update_handled_turn(turn_record.indexed_event_ids, terminal_record)
        self.release_pending_turn_claim(turn_record)

    def is_handled(self, event_id: str) -> bool:
        """Return whether one source event already has a terminal outcome."""
        return self._ledger.has_responded(event_id)

    def visible_echo_for_source(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo for one source event."""
        return self._ledger.get_visible_echo_event_id(source_event_id)

    def record_visible_echo(self, source_event_id: str, echo_event_id: str) -> None:
        """Track a visible echo without changing an existing completion outcome."""

        def visible_echo_record(existing_records: Mapping[str, TurnRecord]) -> TurnRecord:
            turn_record = (
                existing_records[source_event_id]
                if source_event_id in existing_records
                else TurnRecord.create([source_event_id], completed=False)
            )
            return replace(turn_record, visible_echo_event_id=echo_event_id)

        self._ledger.update_handled_turn((source_event_id,), visible_echo_record)

    def visible_echo_for_sources(self, source_event_ids: tuple[str, ...]) -> str | None:
        """Return the first visible echo already tracked for one or more source events."""
        return self._ledger.visible_echo_event_id_for_sources(source_event_ids)

    def get_turn_record(self, source_event_id: str) -> TurnRecord | None:
        """Return the ledger-backed canonical record for one source event."""
        return self._ledger.get_turn_record(source_event_id)

    def record_pending_turn(self, turn_record: TurnRecord) -> TurnRecord | None:
        """Persist exact response context before generation reaches session storage."""
        if not turn_record.source_event_ids:
            return None
        pending_record = replace(turn_record, completed=False, timestamp=0.0)

        def merge_pending(existing_records: Mapping[str, TurnRecord]) -> TurnRecord:
            compatible_existing_records = tuple(
                existing
                for existing in existing_records.values()
                if not existing.completed or same_turn_identity(existing, pending_record)
            )
            existing_record = max(
                compatible_existing_records,
                key=lambda record: (record.completed, record.timestamp),
                default=None,
            )
            merged_record = (
                _backfill_missing_turn_facts(pending_record, existing_record)
                if existing_record is not None
                else pending_record
            )
            redacted_source_event_ids, pending_redaction_cleanup_event_ids = _merged_redaction_markers(
                pending_record,
                merged_record,
                compatible_existing_records,
            )
            if _has_redaction_cleanup_context(merged_record):
                pending_event_ids = set(pending_redaction_cleanup_event_ids)
                pending_event_ids.update(redacted_source_event_ids)
                pending_redaction_cleanup_event_ids = tuple(
                    event_id for event_id in merged_record.indexed_event_ids if event_id in pending_event_ids
                )
            return replace(
                merged_record,
                completed=False,
                redacted_source_event_ids=redacted_source_event_ids,
                pending_redaction_cleanup_event_ids=pending_redaction_cleanup_event_ids,
                timestamp=0.0,
            )

        return self._ledger.update_handled_turn(
            pending_record.indexed_event_ids,
            merge_pending,
            wait_for_persist=True,
        )

    def try_claim_turn(self, turn_record: TurnRecord) -> bool:
        """Claim source processing before any expensive dispatch resolution."""
        event_ids = turn_record.indexed_event_ids
        if not turn_record.source_event_ids:
            return False
        with self._pending_claim_lock:
            if self._pending_claimed_event_ids.intersection(event_ids):
                return False
            self._pending_claimed_event_ids.update(event_ids)
        return True

    def release_pending_turn_claim(self, turn_record: TurnRecord) -> None:
        """Release a response claim after terminal settlement or failure."""
        with self._pending_claim_lock:
            self._pending_claimed_event_ids.difference_update(turn_record.indexed_event_ids)

    def mark_source_redacted(
        self,
        source_event_id: str,
    ) -> TurnRecord | None:
        """Durably tombstone one source event before later replay cleanup."""

        def redacted_record(existing_records: Mapping[str, TurnRecord]) -> TurnRecord:
            existing_record = existing_records.get(source_event_id)
            authority = existing_record or TurnRecord.create([source_event_id], completed=False)
            pending_redaction_cleanup_event_ids = authority.pending_redaction_cleanup_event_ids
            if _has_redaction_cleanup_context(authority):
                pending_redaction_cleanup_event_ids = (
                    *pending_redaction_cleanup_event_ids,
                    source_event_id,
                )
            return replace(
                authority,
                redacted_source_event_ids=(*authority.redacted_source_event_ids, source_event_id),
                pending_redaction_cleanup_event_ids=pending_redaction_cleanup_event_ids,
                timestamp=0.0,
            )

        return self._ledger.update_handled_turn(
            (source_event_id,),
            redacted_record,
            wait_for_persist=True,
        )

    def any_source_redacted(self, source_event_ids: tuple[str, ...]) -> bool:
        """Return whether durable state tombstones any source in one pending response."""
        return any(
            (record := self._ledger.get_turn_record(source_event_id)) is not None
            and source_event_id in record.redacted_source_event_ids
            for source_event_id in source_event_ids
        )

    def prepare_response_for_redactions(
        self,
        *,
        target: MessageTarget,
        source_event_ids: tuple[str, ...],
    ) -> bool:
        """Finish owed cleanup in this locked conversation, then check current sources."""
        for redacted_event_id in self._ledger.pending_redaction_cleanup_event_ids():
            turn_record = self._ledger.get_turn_record(redacted_event_id)
            if turn_record is None:
                continue
            recorded_target = turn_record.conversation_target
            recorded_requester_user_id = turn_record.requester_id
            if not _has_redaction_cleanup_context(turn_record):
                self._clear_pending_redaction_cleanup(redacted_event_id)
                continue
            assert recorded_target is not None
            assert recorded_requester_user_id is not None
            if recorded_target.session_id != target.session_id:
                continue
            self._remove_redacted_event_from_recorded_scopes(
                target=recorded_target,
                requester_user_id=recorded_requester_user_id,
                redacted_event_id=redacted_event_id,
            )
            self._clear_pending_redaction_cleanup(redacted_event_id)
        return self.any_source_redacted(source_event_ids)

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
        additional_discovery_event_ids: tuple[str, ...] = (),
    ) -> dict[str, Any] | None:
        """Project one record into versioned recoverable Agno run metadata.

        ``additional_discovery_event_ids`` lets one anchored run stay discoverable by
        extra triggering events, such as a numeric interactive reply whose response
        still anchors to the original question event.
        """
        projected_record = turn_record
        if additional_discovery_event_ids:
            projected_record = replace(
                turn_record,
                discovery_event_ids=(*turn_record.discovery_event_ids, *additional_discovery_event_ids),
            )
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
        ledger_record_before_recovery = self._ledger.get_turn_record(original_event_id)
        if not self.deps.state_writer.supports_run_recovery():
            return ledger_record_before_recovery
        recovery_record = self._load_persisted_turn_record(
            _LoadPersistedTurnRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
        )
        if recovery_record is None:
            return self._ledger.get_turn_record(original_event_id)

        def repaired_record(existing_records: Mapping[str, TurnRecord]) -> TurnRecord:
            ledger_record = existing_records.get(original_event_id)
            return (
                _reconcile_ledger_and_recovery(
                    ledger_record,
                    recovery_record,
                    recovery_may_replace=ledger_record == ledger_record_before_recovery,
                )
                if ledger_record is not None
                else recovery_record
            )

        return self._ledger.update_handled_turn(
            (original_event_id, *recovery_record.indexed_event_ids),
            repaired_record,
        )

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
            reason="edited",
        )

    def _remove_redacted_event_from_recorded_scopes(
        self,
        *,
        target: MessageTarget,
        requester_user_id: str,
        redacted_event_id: str,
    ) -> bool:
        """Remove causal replay from every self-owned scope in one conversation."""
        candidate_records = self._ledger.turn_records_for_conversation(session_id=target.session_id)
        fallback_scope = self.deps.state_writer.history_scope()
        contexts: dict[tuple[str, str, str], tuple[MessageTarget, HistoryScope, str]] = {
            (target.session_id, fallback_scope.key, requester_user_id): (
                target,
                fallback_scope,
                requester_user_id,
            ),
        }
        for candidate in candidate_records:
            if (
                candidate.response_owner != self.deps.agent_name
                or candidate.requester_id is None
                or candidate.conversation_target is None
                or candidate.history_scope is None
            ):
                continue
            key = (
                candidate.conversation_target.session_id,
                candidate.history_scope.key,
                candidate.requester_id,
            )
            contexts[key] = (
                candidate.conversation_target,
                candidate.history_scope,
                candidate.requester_id,
            )

        removed_any = False
        for candidate_target, history_scope, candidate_requester_id in contexts.values():
            removed = self._remove_redacted_event_from_scope(
                target=candidate_target,
                history_scope=history_scope,
                requester_user_id=candidate_requester_id,
                redacted_event_id=redacted_event_id,
            )
            removed_any = removed or removed_any
        return removed_any

    def _clear_pending_redaction_cleanup(self, redacted_event_id: str) -> None:
        """Acknowledge one cleanup intent after its conversation has been cleaned."""

        def cleared_record(existing_records: Mapping[str, TurnRecord]) -> TurnRecord:
            turn_record = existing_records[redacted_event_id]
            return replace(
                turn_record,
                pending_redaction_cleanup_event_ids=tuple(
                    event_id
                    for event_id in turn_record.pending_redaction_cleanup_event_ids
                    if event_id != redacted_event_id
                ),
                timestamp=0.0,
            )

        if self._ledger.get_turn_record(redacted_event_id) is None:
            return
        self._ledger.update_handled_turn((redacted_event_id,), cleared_record)

    def _remove_redacted_event_from_scope(
        self,
        *,
        target: MessageTarget,
        history_scope: HistoryScope,
        requester_user_id: str,
        redacted_event_id: str,
    ) -> bool:
        """Remove source-backed replay from one source-derived fallback scope."""
        execution_identity = self.deps.tool_runtime.build_execution_identity(
            target=target,
            user_id=requester_user_id,
        )
        storage = self.deps.state_writer.create_storage(execution_identity, scope=history_scope)
        session_type = self.deps.state_writer.session_type_for_scope(history_scope)
        try:
            removed_run = remove_run_by_event_id(
                storage,
                target.session_id,
                redacted_event_id,
                session_type=session_type,
                include_seen_event_ids=True,
                remove_following_runs=True,
            )
            session = (
                get_team_session(storage, target.session_id)
                if session_type is SessionType.TEAM
                else get_agent_session(storage, target.session_id)
            )
            scope_contains_source = session is not None and redacted_event_id in read_scope_seen_event_ids(
                session,
                history_scope,
            )
            removed_summary_dependents = bool(
                session is not None and session.summary is not None and scope_contains_source and session.runs,
            )
            if removed_summary_dependents:
                assert session is not None
                session.runs = []
            invalidated_summary = False
            if session is not None and (removed_run or scope_contains_source):
                invalidated_summary = invalidate_compacted_replay(session, history_scope)
                if invalidated_summary or removed_summary_dependents:
                    storage.upsert_session(session)
            return removed_run or removed_summary_dependents or invalidated_summary
        finally:
            storage.close()

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
                and original_event_id not in turn_record.indexed_event_ids
            ):
                continue
            run_created_at = (
                run.created_at
                if isinstance(run.created_at, int | float) and not isinstance(run.created_at, bool)
                else 0
            )
            sort_key = (run_created_at, run_index)
            if newest_match is None or sort_key > newest_match[0]:
                newest_match = (sort_key, replace(turn_record, timestamp=float(run_created_at)))
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
        reason: str,
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
            for source_event_id in turn_record.indexed_event_ids:
                removed_source = remove_run_by_event_id(
                    storage,
                    session_id,
                    source_event_id,
                    session_type=session_type,
                    remove_following_runs=True,
                )
                removed_any = removed_source or removed_any
        finally:
            storage.close()
        if removed_any:
            self.deps.state_writer.deps.logger.info(
                "Removed stale persisted history for handled turn",
                reason=reason,
                source_event_ids=list(turn_record.source_event_ids),
                session_id=session_id,
                history_scope=turn_record.history_scope.key,
            )
        return removed_any


def _merged_redaction_markers(
    candidate: TurnRecord,
    merged_record: TurnRecord,
    compatible_existing_records: tuple[TurnRecord, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Merge tombstones and pending cleanup markers across compatible aliases."""
    redacted_event_ids = set(candidate.redacted_source_event_ids)
    pending_cleanup_event_ids = set(candidate.pending_redaction_cleanup_event_ids)
    for existing in compatible_existing_records:
        redacted_event_ids.update(existing.redacted_source_event_ids)
        pending_cleanup_event_ids.update(existing.pending_redaction_cleanup_event_ids)
    merged_redacted_event_ids = tuple(
        event_id for event_id in merged_record.indexed_event_ids if event_id in redacted_event_ids
    )
    merged_pending_event_ids = tuple(
        event_id for event_id in merged_record.indexed_event_ids if event_id in pending_cleanup_event_ids
    )
    return merged_redacted_event_ids, merged_pending_event_ids


def _has_redaction_cleanup_context(turn_record: TurnRecord) -> bool:
    """Return whether one record identifies the conversation to sanitize."""
    return (
        turn_record.requester_id is not None
        and turn_record.history_scope is not None
        and turn_record.conversation_target is not None
    )


def _backfill_missing_turn_facts(authority: TurnRecord, recovery: TurnRecord) -> TurnRecord:
    """Fill absent optional facts without overriding authoritative ledger values.

    Source identity, anchor, completion, and timestamp always come from
    ``authority``. Every optional fact uses ``recovery`` only when the
    authoritative value is absent.
    """
    return replace(
        authority,
        discovery_event_ids=(*authority.discovery_event_ids, *recovery.discovery_event_ids),
        redacted_source_event_ids=(
            *authority.redacted_source_event_ids,
            *recovery.redacted_source_event_ids,
        ),
        pending_redaction_cleanup_event_ids=(
            *authority.pending_redaction_cleanup_event_ids,
            *recovery.pending_redaction_cleanup_event_ids,
        ),
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


def _reconcile_ledger_and_recovery(
    ledger_record: TurnRecord,
    recovery_record: TurnRecord,
    *,
    recovery_may_replace: bool,
) -> TurnRecord:
    """Keep ledger identity while accepting a newer delivered run's mutable facts."""
    if (
        not recovery_may_replace
        or recovery_record.timestamp < int(ledger_record.timestamp)
        or recovery_record.response_event_id is None
        or not same_turn_identity(ledger_record, recovery_record)
    ):
        backfilled_record = _backfill_missing_turn_facts(ledger_record, recovery_record)
        return (
            replace(
                backfilled_record,
                timestamp=math.nextafter(ledger_record.timestamp, math.inf),
            )
            if backfilled_record != ledger_record
            else ledger_record
        )
    recovered_record = replace(
        ledger_record,
        discovery_event_ids=(*ledger_record.discovery_event_ids, *recovery_record.discovery_event_ids),
        redacted_source_event_ids=(
            *ledger_record.redacted_source_event_ids,
            *recovery_record.redacted_source_event_ids,
        ),
        response_event_id=recovery_record.response_event_id,
        completed=recovery_record.completed,
        source_event_prompts=recovery_record.source_event_prompts or ledger_record.source_event_prompts,
        source_event_metadata=recovery_record.source_event_metadata or ledger_record.source_event_metadata,
        response_owner=recovery_record.response_owner or ledger_record.response_owner,
        requester_id=recovery_record.requester_id or ledger_record.requester_id,
        correlation_id=recovery_record.correlation_id or ledger_record.correlation_id,
        history_scope=recovery_record.history_scope or ledger_record.history_scope,
        conversation_target=recovery_record.conversation_target or ledger_record.conversation_target,
    )
    return (
        replace(
            recovered_record,
            timestamp=max(recovery_record.timestamp, math.nextafter(ledger_record.timestamp, math.inf)),
        )
        if recovered_record != ledger_record
        else ledger_record
    )
