"""Unified durable turn ownership for runtime flows."""

from __future__ import annotations

import asyncio
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
from mindroom.handled_turns import (
    HandledTurnLedger,
    TerminalEditCheckpoint,
    TurnRecord,
    TurnRecordCodec,
    merge_edit_facts,
    same_turn_identity,
)
from mindroom.history.storage import invalidate_compacted_replay, read_scope_seen_event_ids
from mindroom.session_ids import create_session_id

_INTERRUPTED_REPLAY_STATE_KEY = "mindroom_replay_state"
_INTERRUPTED_REPLAY_STATE = "interrupted"

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import nio

    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.history.types import HistoryScope
    from mindroom.message_target import MessageTarget
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport
    from mindroom.turn_policy import ResponseAction


class _TerminalCheckpointConflictError(RuntimeError):
    """Reject a checkpoint mutation without changing canonical turn state."""


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
        """Load the ledger before asynchronous startup recovery begins."""
        self._ledger.warm()

    def flush(self) -> None:
        """Wait until every handled-turn update queued so far is durable."""
        self._ledger.flush()

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
            return replace(
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

    def turn_for_event(self, event_id: str) -> TurnRecord | None:
        """Resolve a canonical turn by source, alias, or visible response ID."""
        return self._ledger.get_turn_record(event_id) or self._ledger.get_turn_record_for_response_event(event_id)

    def commit_terminal_checkpoint(
        self,
        turn_record: TurnRecord,
        *,
        response_event_id: str,
        checkpoint: TerminalEditCheckpoint,
        regeneration_turn_record: TurnRecord | None = None,
    ) -> TurnRecord | None:
        """Make one exact terminal edit the durable canonical turn outcome."""
        if not turn_record.source_event_ids or not response_event_id:
            return None

        def committed_records(
            existing_records: Mapping[str, TurnRecord],
            target_owners: tuple[TurnRecord, ...],
        ) -> tuple[TurnRecord, ...]:
            target_record = existing_records.get(response_event_id)
            if target_record is not None and response_event_id in target_record.redacted_source_event_ids:
                raise _TerminalCheckpointConflictError
            authority = existing_records.get(turn_record.source_event_ids[0])
            if (
                authority is None
                or any(existing_records.get(event_id) != authority for event_id in turn_record.indexed_event_ids)
                or authority.indexed_event_ids != turn_record.indexed_event_ids
                or not same_turn_identity(authority, turn_record)
                or (
                    regeneration_turn_record is None
                    and any(
                        event_id in authority.redacted_source_event_ids for event_id in turn_record.indexed_event_ids
                    )
                )
            ):
                raise _TerminalCheckpointConflictError
            existing_checkpoint = authority.terminal_edit_checkpoint
            if existing_checkpoint is not None and existing_checkpoint.transaction_id == checkpoint.transaction_id:
                return (authority,)
            committed_checkpoint = replace(
                checkpoint,
                accepted_redacted_source_event_ids=(
                    authority.redacted_source_event_ids if regeneration_turn_record is not None else ()
                ),
            )
            committed_authority = _terminal_checkpoint_authority(
                authority,
                target_owners,
                response_event_id=response_event_id,
                checkpoint=committed_checkpoint,
                regeneration_turn_record=regeneration_turn_record,
            )
            superseded_owners = tuple(
                replace(
                    owner,
                    response_event_id=None,
                    terminal_edit_checkpoint=None,
                    timestamp=0.0,
                )
                for owner in target_owners
                if owner != authority
            )
            return (
                *superseded_owners,
                replace(
                    committed_authority,
                    completed=True,
                    response_event_id=response_event_id,
                    terminal_edit_checkpoint=committed_checkpoint,
                    settled_terminal_delivery_correlation_id=None,
                    timestamp=0.0,
                ),
            )

        try:
            committed = self._ledger.transact_handled_turns(
                turn_record.indexed_event_ids,
                committed_records,
                response_event_id=response_event_id,
            )
        except _TerminalCheckpointConflictError:
            return None
        return next(
            (record for record in committed if same_turn_identity(record, turn_record)),
            None,
        )

    def terminal_checkpoint_records(self) -> tuple[TurnRecord, ...]:
        """Return unique canonical turns still owning terminal checkpoint work."""
        return self._ledger.terminal_checkpoint_records()

    def terminal_checkpoint_for_sources(self, source_event_ids: tuple[str, ...]) -> TurnRecord | None:
        """Return one checkpoint only when every candidate ID belongs to its owner."""
        if not source_event_ids:
            return None
        owner = self._ledger.get_turn_record(source_event_ids[0])
        if (
            owner is None
            or owner.terminal_edit_checkpoint is None
            or any(self._ledger.get_turn_record(event_id) != owner for event_id in source_event_ids)
            or not set(source_event_ids).issubset(owner.indexed_event_ids)
        ):
            return None
        return owner

    def update_terminal_checkpoint(
        self,
        turn_record: TurnRecord,
        *,
        expected_transaction_id: str,
        update: Callable[[TerminalEditCheckpoint], TerminalEditCheckpoint],
    ) -> TurnRecord | None:
        """Durably replace one exact checkpoint without permitting stale writers."""
        return self._mutate_terminal_checkpoint(
            turn_record,
            expected_transaction_id=expected_transaction_id,
            update=update,
        )

    def clear_terminal_checkpoint(
        self,
        turn_record: TurnRecord,
        *,
        expected_transaction_id: str,
    ) -> TurnRecord | None:
        """Clear one checkpoint only after its required lifecycle state converges."""

        def clear(checkpoint: TerminalEditCheckpoint) -> None:
            if not checkpoint.after_response_claimed or (
                checkpoint.interactive_metadata is not None and not checkpoint.interactive_completed
            ):
                raise _TerminalCheckpointConflictError

        return self._mutate_terminal_checkpoint(
            turn_record,
            expected_transaction_id=expected_transaction_id,
            update=clear,
        )

    def clear_redacted_terminal_checkpoint(
        self,
        turn_record: TurnRecord,
        *,
        expected_transaction_id: str,
    ) -> TurnRecord | None:
        """Clear source-redacted checkpoint debt after its visible target is gone."""

        def cleared_records(
            existing_records: Mapping[str, TurnRecord],
            _response_owners: tuple[TurnRecord, ...],
        ) -> tuple[TurnRecord, ...]:
            authority = existing_records.get(turn_record.source_event_ids[0])
            if (
                authority is None
                or authority.indexed_event_ids != turn_record.indexed_event_ids
                or not authority.redacted_source_event_ids
                or authority.terminal_edit_checkpoint is None
                or authority.terminal_edit_checkpoint.transaction_id != expected_transaction_id
            ):
                raise _TerminalCheckpointConflictError
            return (
                replace(
                    authority,
                    response_event_id=None,
                    terminal_edit_checkpoint=None,
                    settled_terminal_delivery_correlation_id=None,
                    timestamp=0.0,
                ),
            )

        try:
            records = self._ledger.transact_handled_turns(
                turn_record.indexed_event_ids,
                cleared_records,
                response_event_id=turn_record.response_event_id,
            )
        except _TerminalCheckpointConflictError:
            return None
        return records[0] if records else None

    def _mutate_terminal_checkpoint(
        self,
        turn_record: TurnRecord,
        *,
        expected_transaction_id: str,
        update: Callable[[TerminalEditCheckpoint], TerminalEditCheckpoint | None],
    ) -> TurnRecord | None:
        """Apply one transaction-fenced checkpoint mutation."""

        def updated_records(
            existing_records: Mapping[str, TurnRecord],
            _response_owners: tuple[TurnRecord, ...],
        ) -> tuple[TurnRecord, ...]:
            authority = next(
                (record for record in existing_records.values() if same_turn_identity(record, turn_record)),
                None,
            )
            if authority is None:
                raise _TerminalCheckpointConflictError
            checkpoint = authority.terminal_edit_checkpoint
            if checkpoint is None or checkpoint.transaction_id != expected_transaction_id:
                raise _TerminalCheckpointConflictError
            updated = update(checkpoint)
            if updated is not None and updated.transaction_id != expected_transaction_id:
                raise _TerminalCheckpointConflictError
            return (
                replace(
                    authority,
                    terminal_edit_checkpoint=updated,
                    settled_terminal_delivery_correlation_id=(
                        checkpoint.correlation_id
                        if updated is None
                        else authority.settled_terminal_delivery_correlation_id
                    ),
                    timestamp=0.0,
                ),
            )

        try:
            records = self._ledger.transact_handled_turns(
                turn_record.indexed_event_ids,
                updated_records,
            )
        except _TerminalCheckpointConflictError:
            return None
        return records[0] if records else None

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
        *,
        fallback_terminal_checkpoint: TerminalEditCheckpoint | None = None,
        fallback_response_event_id: str | None = None,
    ) -> TurnRecord | None:
        """Durably tombstone one source event before later replay cleanup."""

        def redacted_records(
            existing_records: Mapping[str, TurnRecord],
            response_owners: tuple[TurnRecord, ...],
        ) -> tuple[TurnRecord, ...]:
            if response_owners:
                target_tombstone = TurnRecord.create(
                    [source_event_id],
                    redacted_source_event_ids=[source_event_id],
                    completed=False,
                )
                return (
                    *(
                        replace(
                            existing_records[owner.source_event_ids[0]],
                            response_event_id=None,
                            terminal_edit_checkpoint=None,
                            settled_terminal_delivery_correlation_id=None,
                            timestamp=0.0,
                        )
                        for owner in response_owners
                    ),
                    target_tombstone,
                )
            existing_record = existing_records.get(source_event_id)
            authority = existing_record or TurnRecord.create([source_event_id], completed=False)
            retained_checkpoint = authority.terminal_edit_checkpoint
            retained_response_event_id = authority.response_event_id
            if (
                retained_checkpoint is None
                and fallback_terminal_checkpoint is not None
                and authority.response_event_id == fallback_response_event_id
                and authority.correlation_id == fallback_terminal_checkpoint.correlation_id
            ):
                retained_checkpoint = fallback_terminal_checkpoint
                retained_response_event_id = fallback_response_event_id
            pending_redaction_cleanup_event_ids = authority.pending_redaction_cleanup_event_ids
            if _has_redaction_cleanup_context(authority):
                pending_redaction_cleanup_event_ids = (
                    *pending_redaction_cleanup_event_ids,
                    source_event_id,
                )
            return (
                replace(
                    authority,
                    redacted_source_event_ids=(*authority.redacted_source_event_ids, source_event_id),
                    pending_redaction_cleanup_event_ids=pending_redaction_cleanup_event_ids,
                    response_event_id=retained_response_event_id,
                    terminal_edit_checkpoint=retained_checkpoint,
                    settled_terminal_delivery_correlation_id=(
                        None if retained_checkpoint is not None else authority.settled_terminal_delivery_correlation_id
                    ),
                    timestamp=0.0,
                ),
            )

        redacted = self._ledger.transact_handled_turns(
            (source_event_id,),
            redacted_records,
            response_event_id=source_event_id,
        )
        return redacted[0] if redacted else None

    def any_source_redacted(self, source_event_ids: tuple[str, ...]) -> bool:
        """Return whether durable state tombstones any source in one pending response."""
        return any(
            (record := self._ledger.get_turn_record(source_event_id)) is not None
            and (
                source_event_id in record.redacted_source_event_ids
                or source_event_id in set(record.source_event_ids).difference(record.replay_source_event_ids)
            )
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
            if run.metadata.get(_INTERRUPTED_REPLAY_STATE_KEY) == _INTERRUPTED_REPLAY_STATE:
                turn_record = replace(
                    turn_record,
                    source_event_revisions=None,
                    suppressed_source_event_revisions=None,
                    correlation_id=None,
                )
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


def _strictly_newer_source_revisions(candidate: TurnRecord, authority: TurnRecord) -> bool:
    """Return whether candidate advances revisions without rolling any backward."""
    candidate_revisions = candidate.source_event_revisions or {}
    authority_revisions = authority.source_event_revisions or {}
    advanced = False
    for event_id, authority_revision in authority_revisions.items():
        candidate_revision = candidate_revisions.get(event_id)
        if candidate_revision is None or candidate_revision < authority_revision:
            return False
        advanced = advanced or candidate_revision > authority_revision
    return advanced or any(event_id not in authority_revisions for event_id in candidate_revisions)


def _terminal_checkpoint_authority(
    authority: TurnRecord,
    target_owners: tuple[TurnRecord, ...],
    *,
    response_event_id: str,
    checkpoint: TerminalEditCheckpoint,
    regeneration_turn_record: TurnRecord | None,
) -> TurnRecord:
    """Validate a fresh response or edit-regeneration checkpoint authority."""
    if regeneration_turn_record is None:
        if authority.completed or authority.redacted_source_event_ids:
            raise _TerminalCheckpointConflictError
        return authority
    redacted_prompt_source_ids = {
        authority.prompt_source_event_id(event_id) for event_id in authority.redacted_source_event_ids
    }
    authority_revisions = authority.source_event_revisions or {}
    regeneration_revisions = regeneration_turn_record.source_event_revisions or {}
    if (
        not authority.completed
        or checkpoint.target_was_placeholder
        or authority.response_event_id != response_event_id
        or regeneration_turn_record.response_event_id != response_event_id
        or regeneration_turn_record.indexed_event_ids != authority.indexed_event_ids
        or not same_turn_identity(regeneration_turn_record, authority)
        or regeneration_turn_record.redacted_source_event_ids != authority.redacted_source_event_ids
        or any(
            regeneration_revisions.get(event_id) != authority_revisions.get(event_id)
            for event_id in redacted_prompt_source_ids
        )
        or regeneration_turn_record.correlation_id != checkpoint.correlation_id
        or not _strictly_newer_source_revisions(regeneration_turn_record, authority)
        or any(owner != authority for owner in target_owners)
    ):
        raise _TerminalCheckpointConflictError
    return replace(
        authority,
        source_event_prompts=regeneration_turn_record.source_event_prompts,
        source_event_revisions=regeneration_turn_record.source_event_revisions,
        suppressed_source_event_revisions=regeneration_turn_record.suppressed_source_event_revisions,
        correlation_id=regeneration_turn_record.correlation_id,
    )


def _has_redaction_cleanup_context(turn_record: TurnRecord) -> bool:
    """Return whether one record identifies the conversation to sanitize."""
    return (
        turn_record.requester_id is not None
        and turn_record.history_scope is not None
        and turn_record.conversation_target is not None
    )


def _backfill_missing_turn_facts(authority: TurnRecord, recovery: TurnRecord) -> TurnRecord:
    """Fill absent optional facts from recovery without overriding ledger authority."""
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
        terminal_edit_checkpoint=authority.terminal_edit_checkpoint or recovery.terminal_edit_checkpoint,
        settled_terminal_delivery_correlation_id=(
            authority.settled_terminal_delivery_correlation_id or recovery.settled_terminal_delivery_correlation_id
        ),
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
        recovery_record = replace(recovery_record, source_event_revisions=None)
        backfilled_record = _backfill_missing_turn_facts(ledger_record, recovery_record)
        return (
            replace(
                backfilled_record,
                timestamp=math.nextafter(ledger_record.timestamp, math.inf),
            )
            if backfilled_record != ledger_record
            else ledger_record
        )
    source_event_prompts, source_event_revisions = merge_edit_facts(ledger_record, recovery_record)
    recovered_record = replace(
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
