"""Unified durable turn ownership for runtime flows."""

from __future__ import annotations

import asyncio
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput

from mindroom.agent_storage import get_agent_session, get_team_session
from mindroom.agents import remove_run_by_event_id
from mindroom.handled_turns import (
    HandledTurnLedger,
    TurnRecord,
    TurnRecordCodec,
    merge_edit_facts,
    same_turn_identity,
    with_user_stop,
)
from mindroom.history.storage import invalidate_compacted_replay, read_scope_seen_event_ids
from mindroom.session_ids import create_session_id
from mindroom.turn_record import canonicalize_turn_record

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping

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
    on_terminal_turn_persisted: Callable[[tuple[str, ...]], None] | None = None


@dataclass(frozen=True)
class _FinalizedVisibleEcho:
    """Durable terminal state for one editable visible echo."""

    event_id: str
    is_fallback: bool


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
    _pending_claim_changed: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _pending_turn_claims: list[TurnRecord] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        """Construct the private handled-turn ledger for this runtime entity."""
        self._ledger = HandledTurnLedger(
            self.deps.agent_name,
            base_path=Path(self.deps.tracking_base_path),
        )

    def warm(self) -> None:
        """Load the ledger without pruning truth needed by startup recovery."""
        self._ledger.load()

    def cleanup(self, *, unsettled_source_event_ids: Collection[str] = ()) -> None:
        """Compact terminal history after startup recovery identifies live sources."""
        self._ledger.cleanup(unsettled_source_event_ids=unsettled_source_event_ids)

    def record_turn(self, turn_record: TurnRecord) -> None:
        """Persist one terminal turn, preserving any previously recorded optional facts."""
        self._record_terminal_turn(turn_record, wait_for_persist=False)

    def record_responded_turn(self, turn_record: TurnRecord) -> None:
        """Persist a terminal turn that owns a visible Matrix response."""
        if not turn_record.response_event_id:
            msg = "A responded turn requires a visible Matrix response event ID"
            raise RuntimeError(msg)
        self.record_turn(turn_record)

    def record_turn_durably(self, turn_record: TurnRecord) -> None:
        """Persist one terminal turn and wait until its exact ledger write lands."""
        self._record_terminal_turn(turn_record, wait_for_persist=True)

    def _record_terminal_turn(self, turn_record: TurnRecord, *, wait_for_persist: bool) -> None:
        """Apply the canonical terminal merge with optional exact durability."""
        turn_record = canonicalize_turn_record(turn_record)
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
            return canonicalize_turn_record(
                merged_record,
                completed=True,
                redacted_source_event_ids=redacted_source_event_ids,
                pending_redaction_cleanup_event_ids=pending_redaction_cleanup_event_ids,
                visible_echo_event_id=visible_echo_event_id,
                timestamp=0.0,
            )

        self._ledger.update_handled_turn(
            turn_record.indexed_event_ids,
            terminal_record,
            wait_for_persist=wait_for_persist,
            on_persisted=self._notify_terminal_turn_persisted,
        )

    def is_handled(self, event_id: str) -> bool:
        """Return whether one source event already has a terminal outcome."""
        return self._ledger.has_responded(event_id)

    def is_durably_handled(self, event_id: str) -> bool:
        """Return terminal truth only after its handled-turn ledger write completes."""
        return self._ledger.has_durably_responded(event_id)

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
            return canonicalize_turn_record(turn_record, visible_echo_event_id=echo_event_id)

        self._ledger.update_handled_turn((source_event_id,), visible_echo_record)

    def record_finalized_visible_echo(
        self,
        source_event_id: str,
        echo_event_id: str,
        *,
        is_fallback: bool,
    ) -> None:
        """Mark a tracked visible echo as successfully replaced."""
        tracked_record = self.get_turn_record(source_event_id)
        if tracked_record is None or tracked_record.visible_echo_event_id != echo_event_id:
            return

        def finalized_visible_echo_record(existing_records: Mapping[str, TurnRecord]) -> TurnRecord:
            existing = existing_records[source_event_id]
            if existing.visible_echo_event_id != echo_event_id or (
                existing.visible_echo_is_fallback is False and is_fallback
            ):
                return existing
            return canonicalize_turn_record(
                existing,
                response_event_id=existing.response_event_id if existing.completed else echo_event_id,
                visible_echo_is_fallback=is_fallback,
                timestamp=0.0,
            )

        self._ledger.update_handled_turn((source_event_id,), finalized_visible_echo_record)

    def finalized_visible_echo(self, source_event_id: str) -> _FinalizedVisibleEcho | None:
        """Return named terminal state for one tracked visible echo."""
        record = self.get_turn_record(source_event_id)
        if record is None or record.visible_echo_event_id is None or record.visible_echo_is_fallback is None:
            return None
        return _FinalizedVisibleEcho(
            event_id=record.visible_echo_event_id,
            is_fallback=record.visible_echo_is_fallback,
        )

    def finalized_visible_echo_for_sources(self, source_event_ids: tuple[str, ...]) -> str | None:
        """Return the first visible echo whose replacement succeeded."""
        for source_event_id in source_event_ids:
            finalized = self.finalized_visible_echo(source_event_id)
            if finalized is not None:
                return finalized.event_id
        return None

    def get_turn_record(self, source_event_id: str) -> TurnRecord | None:
        """Return the ledger-backed canonical record for one source event."""
        return self._ledger.get_turn_record(source_event_id)

    def turn_record_for_response_event_id(self, response_event_id: str) -> TurnRecord | None:
        """Return the durable turn that owns one visible response event."""
        return self._ledger.turn_record_for_response_event_id(response_event_id)

    def _update_response_turn(
        self,
        response_event_id: str,
        update: Callable[[TurnRecord], TurnRecord],
        *,
        notify_terminal: bool = False,
    ) -> TurnRecord | None:
        """Durably update the sole turn that owns one visible response."""
        turn_record = self.turn_record_for_response_event_id(response_event_id)
        if turn_record is None:
            return None

        def updated_record(existing_records: Mapping[str, TurnRecord]) -> TurnRecord:
            matching_records = {
                record.indexed_event_ids: record
                for record in existing_records.values()
                if response_event_id in {record.response_event_id, record.visible_echo_event_id}
            }
            if len(matching_records) != 1:
                msg = f"Response {response_event_id!r} lost its sole turn owner"
                raise RuntimeError(msg)
            return update(next(iter(matching_records.values())))

        return self._ledger.update_handled_turn(
            turn_record.indexed_event_ids,
            updated_record,
            wait_for_persist=True,
            on_persisted=self._notify_terminal_turn_persisted if notify_terminal else None,
        )

    def record_user_stopped_response(
        self,
        response_event_id: str,
        stop_receipt_order: int,
        *,
        delivery_settled: bool = False,
    ) -> TurnRecord | None:
        """Durably terminate the turn that owns a user-stopped response."""
        if isinstance(stop_receipt_order, bool) or stop_receipt_order <= 0:
            msg = "User-stop receipt order must be positive"
            raise ValueError(msg)
        turn_record = self.turn_record_for_response_event_id(response_event_id)
        if turn_record is None:
            return turn_record

        def stopped_record(current: TurnRecord) -> TurnRecord:
            return with_user_stop(
                current,
                response_event_id,
                stop_receipt_order,
                delivery_settled=delivery_settled,
            )

        return self._update_response_turn(
            response_event_id,
            stopped_record,
            notify_terminal=not turn_record.completed,
        )

    def has_pending_response_intent(self, source_event_ids: tuple[str, ...]) -> bool:
        """Return whether these sources already own an incomplete response attempt."""
        return any(
            (record := self.get_turn_record(source_event_id)) is not None
            and not record.completed
            and record.response_owner is not None
            and record.conversation_target is not None
            for source_event_id in source_event_ids
        )

    def record_pending_turn(self, turn_record: TurnRecord) -> TurnRecord | None:
        """Persist exact response context before generation reaches session storage."""
        if not turn_record.source_event_ids:
            return None
        pending_record = canonicalize_turn_record(turn_record, completed=False, timestamp=0.0)

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
            return canonicalize_turn_record(
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

    def _notify_terminal_turn_persisted(self, turn_record: TurnRecord) -> None:
        callback = self.deps.on_terminal_turn_persisted
        if callback is not None:
            callback(turn_record.indexed_event_ids)

    def try_claim_turn(self, turn_record: TurnRecord) -> bool:
        """Claim exclusive physical sources while aliases remain advisory."""
        alias_owners = map(self.get_turn_record, turn_record.discovery_event_ids)
        if not turn_record.source_event_ids or any(
            owner is not None and owner.completed and not same_turn_identity(owner, turn_record)
            for owner in alias_owners
        ):
            return False
        source_ids, discovery_ids = set(turn_record.source_event_ids), set(turn_record.discovery_event_ids)
        with self._pending_claim_lock:
            if any(
                source_ids.intersection(claim.source_event_ids) or discovery_ids.intersection(claim.discovery_event_ids)
                for claim in self._pending_turn_claims
            ):
                return False
            self._pending_turn_claims.append(turn_record)
        return True

    def release_pending_turn_claim(self, turn_record: TurnRecord) -> None:
        """Release a response claim after terminal settlement or failure."""
        with self._pending_claim_lock:
            self._pending_turn_claims = [claim for claim in self._pending_turn_claims if claim != turn_record]
            claim_changed, self._pending_claim_changed = self._pending_claim_changed, asyncio.Event()
        claim_changed.set()

    async def wait_for_turn_settled(self, event_ids: tuple[str, ...]) -> None:
        """Wait until every claim indexed by a source or alias settles."""
        event_id_set = set(event_ids)
        while True:
            with self._pending_claim_lock:
                if not any(event_id_set.intersection(claim.indexed_event_ids) for claim in self._pending_turn_claims):
                    return
                claim_changed = self._pending_claim_changed
            await claim_changed.wait()

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
            return canonicalize_turn_record(
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

    def _any_source_redacted(self, source_event_ids: tuple[str, ...]) -> bool:
        """Return whether durable state tombstones any source in one pending response."""
        return any(
            (record := self._ledger.get_turn_record(source_event_id)) is not None
            and (
                source_event_id in record.redacted_source_event_ids
                or source_event_id in set(record.source_event_ids).difference(record.replay_source_event_ids)
            )
            for source_event_id in source_event_ids
        )

    def _prepare_response_for_redactions(
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
        return self._any_source_redacted(source_event_ids)

    def prepare_pending_response_source(
        self,
        *,
        target: MessageTarget,
        source_event_ids: tuple[str, ...],
        terminal_source_event_ids: tuple[str, ...],
    ) -> bool:
        """Finish cleanup, then suppress a pending response whose source became terminal."""
        return self._prepare_response_for_redactions(
            target=target,
            source_event_ids=source_event_ids,
        ) or any(self.is_handled(source_event_id) for source_event_id in terminal_source_event_ids)

    def prepare_edit_response_source(
        self,
        *,
        target: MessageTarget,
        source_event_ids: tuple[str, ...],
        response_event_id: str | None,
        edit_receipt_order: int,
    ) -> bool:
        """Suppress pre-STOP edits or durably open later edits for visible delivery."""
        if isinstance(edit_receipt_order, bool) or edit_receipt_order <= 0:
            msg = "Edit receipt order must be positive"
            raise ValueError(msg)
        if self._prepare_response_for_redactions(target=target, source_event_ids=source_event_ids):
            return True
        if response_event_id is None:
            return False

        def prepared_record(current: TurnRecord) -> TurnRecord:
            cutoff = current.user_stop_receipt_order
            if cutoff is not None and edit_receipt_order <= cutoff:
                return current
            return canonicalize_turn_record(
                current,
                latest_edit_receipt_order=max(
                    current.latest_edit_receipt_order or 0,
                    edit_receipt_order,
                ),
                user_stop_settled_receipt_order=max(
                    current.user_stop_settled_receipt_order or 0,
                    cutoff or 0,
                )
                or None,
                timestamp=0.0,
            )

        prepared = self._update_response_turn(response_event_id, prepared_record)
        return prepared is None or (
            prepared.user_stop_receipt_order is not None and edit_receipt_order <= prepared.user_stop_receipt_order
        )

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
        return canonicalize_turn_record(
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
            projected_record = canonicalize_turn_record(
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
            return canonicalize_turn_record(
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
                newest_match = (sort_key, canonicalize_turn_record(turn_record, timestamp=float(run_created_at)))
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
    """Fill absent optional facts from recovery without overriding ledger authority."""
    return canonicalize_turn_record(
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
        visible_echo_is_fallback=(
            authority.visible_echo_is_fallback
            if authority.visible_echo_is_fallback is not None
            else recovery.visible_echo_is_fallback
        ),
        source_event_prompts=(
            authority.source_event_prompts
            if authority.source_event_prompts is not None
            else recovery.source_event_prompts
        ),
        source_event_revisions=authority.source_event_revisions or recovery.source_event_revisions,
        latest_edit_receipt_order=_latest_edit_receipt_order(authority, recovery),
        user_stop_receipt_order=_latest_user_stop_receipt_order(authority, recovery),
        user_stop_settled_receipt_order=_latest_user_stop_settled_receipt_order(authority, recovery),
        source_event_metadata=(
            authority.source_event_metadata
            if authority.source_event_metadata is not None
            else recovery.source_event_metadata
        ),
        response_owner=authority.response_owner or recovery.response_owner,
        requester_id=authority.requester_id or recovery.requester_id,
        correlation_id=authority.correlation_id or recovery.correlation_id,
        command_execution_started=authority.command_execution_started or recovery.command_execution_started,
        command_result_text=authority.command_result_text or recovery.command_result_text,
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
        recovery_record = canonicalize_turn_record(recovery_record, source_event_revisions=None)
        backfilled_record = _backfill_missing_turn_facts(ledger_record, recovery_record)
        return (
            canonicalize_turn_record(
                backfilled_record,
                timestamp=math.nextafter(ledger_record.timestamp, math.inf),
            )
            if backfilled_record != ledger_record
            else ledger_record
        )
    source_event_prompts, source_event_revisions = merge_edit_facts(ledger_record, recovery_record)
    recovered_record = canonicalize_turn_record(
        ledger_record,
        discovery_event_ids=(*ledger_record.discovery_event_ids, *recovery_record.discovery_event_ids),
        redacted_source_event_ids=(
            *ledger_record.redacted_source_event_ids,
            *recovery_record.redacted_source_event_ids,
        ),
        response_event_id=recovery_record.response_event_id,
        completed=recovery_record.completed,
        source_event_prompts=source_event_prompts,
        source_event_revisions=source_event_revisions,
        latest_edit_receipt_order=_latest_edit_receipt_order(ledger_record, recovery_record),
        user_stop_receipt_order=_latest_user_stop_receipt_order(ledger_record, recovery_record),
        user_stop_settled_receipt_order=_latest_user_stop_settled_receipt_order(
            ledger_record,
            recovery_record,
        ),
        source_event_metadata=(
            recovery_record.source_event_metadata
            if recovery_record.source_event_metadata is not None
            else ledger_record.source_event_metadata
        ),
        response_owner=recovery_record.response_owner or ledger_record.response_owner,
        requester_id=recovery_record.requester_id or ledger_record.requester_id,
        correlation_id=recovery_record.correlation_id or ledger_record.correlation_id,
        history_scope=recovery_record.history_scope or ledger_record.history_scope,
        conversation_target=recovery_record.conversation_target or ledger_record.conversation_target,
    )
    return (
        canonicalize_turn_record(
            recovered_record,
            timestamp=max(recovery_record.timestamp, math.nextafter(ledger_record.timestamp, math.inf)),
        )
        if recovered_record != ledger_record
        else ledger_record
    )


def _latest_user_stop_receipt_order(*records: TurnRecord) -> int | None:
    """Return the latest durable STOP receipt order on these same-turn records."""
    return max(
        (record.user_stop_receipt_order for record in records if record.user_stop_receipt_order is not None),
        default=None,
    )


def _latest_edit_receipt_order(*records: TurnRecord) -> int | None:
    """Return the latest edit admitted for these same-turn records."""
    return max(
        (record.latest_edit_receipt_order for record in records if record.latest_edit_receipt_order is not None),
        default=None,
    )


def _latest_user_stop_settled_receipt_order(*records: TurnRecord) -> int | None:
    """Return the latest STOP whose visible obligation is delivered or superseded."""
    return max(
        (
            record.user_stop_settled_receipt_order
            for record in records
            if record.user_stop_settled_receipt_order is not None
        ),
        default=None,
    )
