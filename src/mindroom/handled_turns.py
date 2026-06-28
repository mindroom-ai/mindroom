"""Track handled turn outcomes for one agent.

Reads are served from in-memory state shared across every ledger bound to the
same responses file, so sibling ledger instances in one process observe each
other's writes without touching the filesystem. Disk persistence happens on a
single write-behind worker thread that merges applied records into the file
under an advisory lock, keeping cross-process writers safe while the event
loop never blocks on filesystem I/O (issue #1260).
"""

from __future__ import annotations

import json
import threading
import time
import typing
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, NotRequired, TypedDict

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.history import HistoryScope, HistoryScopeMetadata
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget, MessageTargetMetadata

if typing.TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class _SerializedHandledTurnRecord(TypedDict):
    """Record of one handled source event persisted to disk."""

    timestamp: float
    response_event_id: str | None
    completed: NotRequired[bool]
    anchor_event_id: NotRequired[str]
    visible_echo_event_id: NotRequired[str | None]
    source_event_ids: NotRequired[list[str]]
    source_event_prompts: NotRequired[dict[str, str] | None]
    source_event_metadata: NotRequired[dict[str, dict[str, object]] | None]
    response_owner: NotRequired[str | None]
    requester_id: NotRequired[str | None]
    correlation_id: NotRequired[str | None]
    history_scope: NotRequired[HistoryScopeMetadata | None]
    conversation_target: NotRequired[MessageTargetMetadata | None]


type _SerializedHandledTurnRecordLike = _SerializedHandledTurnRecord | dict[str, Any]


@dataclass(frozen=True)
class SourceEventMetadata:
    """Durable model-facing metadata for one source Matrix event."""

    sender: str
    timestamp_ms: float | None = None

    def to_record(self) -> dict[str, object]:
        """Return a JSON-safe representation for durable metadata."""
        record: dict[str, object] = {"sender": self.sender}
        if self.timestamp_ms is not None:
            record["timestamp_ms"] = self.timestamp_ms
        return record

    @classmethod
    def from_raw(cls, raw_metadata: object) -> SourceEventMetadata | None:
        """Build source metadata from a persisted JSON-like value."""
        if not isinstance(raw_metadata, Mapping):
            return None
        metadata = typing.cast("Mapping[str, object]", raw_metadata)
        sender = metadata.get("sender")
        if not isinstance(sender, str) or not sender:
            return None
        return cls(
            sender=sender,
            timestamp_ms=_normalized_source_event_timestamp_ms(metadata.get("timestamp_ms")),
        )


@dataclass(frozen=True)
class HandledTurnState:
    """Typed handled-turn facts carried through normal bot runtime flow."""

    source_event_ids: tuple[str, ...]
    response_event_id: str | None = None
    visible_echo_event_id: str | None = None
    source_event_prompts: dict[str, str] | None = None
    source_event_metadata: dict[str, SourceEventMetadata] | None = None
    response_owner: str | None = None
    requester_id: str | None = None
    correlation_id: str | None = None
    history_scope: HistoryScope | None = None
    conversation_target: MessageTarget | None = None

    @classmethod
    def create(
        cls,
        source_event_ids: typing.Sequence[str],
        *,
        response_event_id: str | None = None,
        visible_echo_event_id: str | None = None,
        source_event_prompts: typing.Mapping[str, str] | None = None,
        source_event_metadata: typing.Mapping[str, object] | None = None,
        response_owner: str | None = None,
        requester_id: str | None = None,
        correlation_id: str | None = None,
        history_scope: HistoryScope | None = None,
        conversation_target: MessageTarget | None = None,
    ) -> HandledTurnState:
        """Normalize one handled-turn state carrier."""
        normalized_source_event_ids = _normalize_source_event_ids(source_event_ids)
        return cls(
            source_event_ids=normalized_source_event_ids,
            response_event_id=_normalized_event_id(response_event_id),
            visible_echo_event_id=_normalized_event_id(visible_echo_event_id),
            source_event_prompts=_explicit_prompt_map_for_sources(
                normalized_source_event_ids,
                source_event_prompts,
            ),
            source_event_metadata=_explicit_source_event_metadata_for_sources(
                normalized_source_event_ids,
                source_event_metadata,
            ),
            response_owner=_normalized_response_owner(response_owner),
            requester_id=_normalized_requester_id(requester_id),
            correlation_id=_normalized_correlation_id(correlation_id),
            history_scope=_normalized_history_scope(history_scope),
            conversation_target=_normalized_conversation_target(conversation_target),
        )

    @classmethod
    def from_source_event_id(
        cls,
        source_event_id: str,
        *,
        response_event_id: str | None = None,
        visible_echo_event_id: str | None = None,
        source_event_prompts: typing.Mapping[str, str] | None = None,
        source_event_metadata: typing.Mapping[str, object] | None = None,
        response_owner: str | None = None,
        requester_id: str | None = None,
        correlation_id: str | None = None,
        history_scope: HistoryScope | None = None,
        conversation_target: MessageTarget | None = None,
    ) -> HandledTurnState:
        """Build handled-turn state for one source event."""
        return cls.create(
            [source_event_id],
            response_event_id=response_event_id,
            visible_echo_event_id=visible_echo_event_id,
            source_event_prompts=source_event_prompts,
            source_event_metadata=source_event_metadata,
            response_owner=response_owner,
            requester_id=requester_id,
            correlation_id=correlation_id,
            history_scope=history_scope,
            conversation_target=conversation_target,
        )

    @classmethod
    def from_persisted_metadata(
        cls,
        source_event_ids: typing.Sequence[str],
        *,
        response_event_id: str | None = None,
        source_event_prompts: typing.Mapping[str, str] | None = None,
        source_event_metadata: typing.Mapping[str, object] | None = None,
        response_owner: str | None = None,
        history_scope_metadata: object,
        conversation_target_metadata: object,
    ) -> HandledTurnState:
        """Build handled-turn state from persisted Matrix run metadata."""
        return cls.create(
            source_event_ids,
            response_event_id=response_event_id,
            source_event_prompts=source_event_prompts,
            source_event_metadata=source_event_metadata,
            response_owner=response_owner,
            history_scope=HistoryScope.from_metadata(history_scope_metadata),
            conversation_target=MessageTarget.from_metadata(conversation_target_metadata),
        )

    @property
    def anchor_event_id(self) -> str:
        """Return the event this turn anchors replies and regeneration to."""
        return self.source_event_ids[-1]

    @property
    def is_coalesced(self) -> bool:
        """Return whether the turn combines multiple source events."""
        return len(self.source_event_ids) > 1

    def with_response_event_id(self, response_event_id: str | None) -> HandledTurnState:
        """Return a copy with updated response linkage."""
        return HandledTurnState.create(
            self.source_event_ids,
            response_event_id=response_event_id,
            visible_echo_event_id=self.visible_echo_event_id,
            source_event_prompts=self.source_event_prompts,
            source_event_metadata=self.source_event_metadata,
            response_owner=self.response_owner,
            requester_id=self.requester_id,
            correlation_id=self.correlation_id,
            history_scope=self.history_scope,
            conversation_target=self.conversation_target,
        )

    def with_visible_echo_event_id(self, visible_echo_event_id: str | None) -> HandledTurnState:
        """Return a copy with updated visible-echo linkage."""
        return HandledTurnState.create(
            self.source_event_ids,
            response_event_id=self.response_event_id,
            visible_echo_event_id=visible_echo_event_id,
            source_event_prompts=self.source_event_prompts,
            source_event_metadata=self.source_event_metadata,
            response_owner=self.response_owner,
            requester_id=self.requester_id,
            correlation_id=self.correlation_id,
            history_scope=self.history_scope,
            conversation_target=self.conversation_target,
        )

    def with_source_event_prompts(
        self,
        source_event_prompts: typing.Mapping[str, str] | None,
    ) -> HandledTurnState:
        """Return a copy with updated coalesced prompt metadata."""
        return HandledTurnState.create(
            self.source_event_ids,
            response_event_id=self.response_event_id,
            visible_echo_event_id=self.visible_echo_event_id,
            source_event_prompts=source_event_prompts,
            source_event_metadata=self.source_event_metadata,
            response_owner=self.response_owner,
            requester_id=self.requester_id,
            correlation_id=self.correlation_id,
            history_scope=self.history_scope,
            conversation_target=self.conversation_target,
        )

    def with_request_context(
        self,
        *,
        requester_id: str | None,
        correlation_id: str | None,
    ) -> HandledTurnState:
        """Return a copy with updated request trace context."""
        return HandledTurnState.create(
            self.source_event_ids,
            response_event_id=self.response_event_id,
            visible_echo_event_id=self.visible_echo_event_id,
            source_event_prompts=self.source_event_prompts,
            source_event_metadata=self.source_event_metadata,
            response_owner=self.response_owner,
            requester_id=requester_id,
            correlation_id=correlation_id,
            history_scope=self.history_scope,
            conversation_target=self.conversation_target,
        )

    def with_response_context(
        self,
        *,
        response_owner: str | None,
        requester_id: str | None = None,
        correlation_id: str | None = None,
        history_scope: HistoryScope | None,
        conversation_target: MessageTarget | None,
    ) -> HandledTurnState:
        """Return a copy with persisted regeneration context attached."""
        return HandledTurnState.create(
            self.source_event_ids,
            response_event_id=self.response_event_id,
            visible_echo_event_id=self.visible_echo_event_id,
            source_event_prompts=self.source_event_prompts,
            source_event_metadata=self.source_event_metadata,
            response_owner=response_owner,
            requester_id=requester_id if requester_id is not None else self.requester_id,
            correlation_id=correlation_id if correlation_id is not None else self.correlation_id,
            history_scope=history_scope,
            conversation_target=conversation_target,
        )


@dataclass(frozen=True)
class HandledTurnRecord:
    """Immutable record for one handled turn."""

    anchor_event_id: str
    source_event_ids: tuple[str, ...]
    response_event_id: str | None = None
    completed: bool = True
    visible_echo_event_id: str | None = None
    source_event_prompts: dict[str, str] | None = None
    source_event_metadata: dict[str, SourceEventMetadata] | None = None
    response_owner: str | None = None
    requester_id: str | None = None
    correlation_id: str | None = None
    history_scope: HistoryScope | None = None
    conversation_target: MessageTarget | None = None
    timestamp: float = 0.0

    @property
    def is_coalesced(self) -> bool:
        """Return whether the turn combined multiple source events."""
        return len(self.source_event_ids) > 1


@dataclass
class _LedgerState:
    """In-memory ledger state shared by every ledger bound to one responses file."""

    responses: dict[str, _SerializedHandledTurnRecord] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    loaded: bool = False
    pending_persists: list[Future[None]] = field(default_factory=list, repr=False)


_LEDGER_STATES: dict[str, _LedgerState] = {}
_LEDGER_RUNTIME_LOCK = threading.Lock()
_PERSIST_EXECUTOR: ThreadPoolExecutor | None = None


def _shared_ledger_state(responses_file: Path) -> _LedgerState:
    """Return the process-wide shared state for one responses file."""
    key = str(responses_file.absolute())
    with _LEDGER_RUNTIME_LOCK:
        state = _LEDGER_STATES.get(key)
        if state is None:
            state = _LedgerState()
            _LEDGER_STATES[key] = state
        return state


def _persist_executor() -> ThreadPoolExecutor:
    """Return the shared single-worker executor that orders ledger persists."""
    global _PERSIST_EXECUTOR
    with _LEDGER_RUNTIME_LOCK:
        if _PERSIST_EXECUTOR is None:
            _PERSIST_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="handled-turn-persist")
        return _PERSIST_EXECUTOR


def _reset_handled_turn_ledger_runtime() -> None:
    """Flush pending persists and drop shared ledger state (tests and forked runtimes)."""
    global _PERSIST_EXECUTOR
    with _LEDGER_RUNTIME_LOCK:
        executor = _PERSIST_EXECUTOR
        _PERSIST_EXECUTOR = None
        _LEDGER_STATES.clear()
    if executor is not None:
        executor.shutdown(wait=True)


@dataclass
class HandledTurnLedger:
    """Track handled source events for one runtime entity."""

    agent_name: str
    base_path: Path
    _responses_file: Path = field(init=False)
    _responses_lock_file: Path = field(init=False)
    _state: _LedgerState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind shared ledger state for this agent without touching the filesystem."""
        self._responses_file = _responses_file_path(self.base_path, self.agent_name)
        self._responses_lock_file = self._responses_file.with_suffix(f"{self._responses_file.suffix}.lock")
        self._state = _shared_ledger_state(self._responses_file)

    @property
    def _responses(self) -> dict[str, _SerializedHandledTurnRecord]:
        return self._state.responses

    @_responses.setter
    def _responses(self, responses: dict[str, _SerializedHandledTurnRecord]) -> None:
        self._state.responses = responses

    def warm(self) -> None:
        """Load and compact the persisted ledger; call from a worker thread, not the event loop."""
        self._cleanup_old_events()

    def flush(self) -> None:
        """Block until every scheduled ledger persist attempt has completed.

        Persist failures are logged and swallowed (best-effort durability), so
        completion means all queued write-behind attempts finished, not that
        every record reached disk.
        """
        with self._state.lock:
            self._wait_for_pending_persists_locked()

    def record_handled_turn(self, handled_turn: HandledTurnState) -> None:
        """Record one handled-turn state as a terminal outcome."""
        normalized_source_event_ids = handled_turn.source_event_ids
        if not normalized_source_event_ids:
            return

        with self._state.lock:
            self._ensure_loaded_locked()
            self._apply_handled_turn_locked(
                source_event_ids=normalized_source_event_ids,
                response_event_id=handled_turn.response_event_id,
                completed=True,
                visible_echo_event_id=handled_turn.visible_echo_event_id,
                source_event_prompts=handled_turn.source_event_prompts,
                source_event_metadata=handled_turn.source_event_metadata,
                response_owner=handled_turn.response_owner,
                requester_id=handled_turn.requester_id,
                correlation_id=handled_turn.correlation_id,
                history_scope=handled_turn.history_scope,
                conversation_target=handled_turn.conversation_target,
            )
            self._schedule_persist_locked(normalized_source_event_ids)
        logger.debug("handled_turn_recorded", source_event_count=len(normalized_source_event_ids))

    def record_handled_turn_record(self, turn_record: HandledTurnRecord) -> None:
        """Record one exact handled-turn record without losing its explicit anchor."""
        normalized_source_event_ids = _normalize_source_event_ids(turn_record.source_event_ids)
        if not normalized_source_event_ids:
            return

        with self._state.lock:
            self._ensure_loaded_locked()
            self._apply_handled_turn_locked(
                normalized_source_event_ids,
                response_event_id=turn_record.response_event_id,
                completed=turn_record.completed,
                visible_echo_event_id=turn_record.visible_echo_event_id,
                source_event_prompts=turn_record.source_event_prompts,
                source_event_metadata=turn_record.source_event_metadata,
                response_owner=turn_record.response_owner,
                requester_id=turn_record.requester_id,
                correlation_id=turn_record.correlation_id,
                history_scope=turn_record.history_scope,
                conversation_target=turn_record.conversation_target,
                anchor_event_id=turn_record.anchor_event_id,
            )
            self._schedule_persist_locked(normalized_source_event_ids)
        logger.debug("handled_turn_recorded", source_event_count=len(normalized_source_event_ids))

    def record_visible_echo(self, source_event_id: str, echo_event_id: str) -> None:
        """Track a visible echo without marking the turn terminally handled."""
        with self._state.lock:
            self._ensure_loaded_locked()
            existing_record = self._responses.get(source_event_id)
            source_event_ids = _source_event_ids_for_record(source_event_id, existing_record)
            self._apply_handled_turn_locked(
                source_event_ids=source_event_ids,
                response_event_id=_response_event_id_for_record(existing_record),
                completed=_completed_for_record(existing_record),
                visible_echo_event_id=echo_event_id,
                source_event_prompts=_prompt_map_for_record(source_event_ids, existing_record),
                source_event_metadata=_source_event_metadata_for_record(source_event_ids, existing_record),
                response_owner=_response_owner_for_record(existing_record),
                requester_id=_requester_id_for_record(existing_record),
                correlation_id=_correlation_id_for_record(existing_record),
                history_scope=_history_scope_for_record(existing_record),
                conversation_target=_conversation_target_for_record(existing_record),
                anchor_event_id=_anchor_event_id_for_record(source_event_ids, existing_record),
            )
            self._schedule_persist_locked(source_event_ids)
        logger.debug(
            "visible_echo_tracked",
            agent=self.agent_name,
            event_id=source_event_id,
            visible_echo_event_id=echo_event_id,
        )

    def has_responded(self, event_id: str) -> bool:
        """Return whether the source event has a terminal recorded outcome."""
        with self._state.lock:
            self._ensure_loaded_locked()
            record = self._responses.get(event_id)
            return bool(record and record.get("completed", True))

    def get_visible_echo_event_id(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo event ID for one source event."""
        with self._state.lock:
            self._ensure_loaded_locked()
            return _visible_echo_event_id_for_record(self._responses.get(source_event_id))

    def visible_echo_event_id_for_sources(self, source_event_ids: typing.Sequence[str]) -> str | None:
        """Return the first visible echo already tracked for one or more source events."""
        normalized_source_event_ids = _normalize_source_event_ids(source_event_ids)
        if not normalized_source_event_ids:
            return None
        with self._state.lock:
            self._ensure_loaded_locked()
            return self._visible_echo_for_sources(normalized_source_event_ids)

    def get_turn_record(self, source_event_id: str) -> HandledTurnRecord | None:
        """Return the handled-turn record for one source event."""
        with self._state.lock:
            self._ensure_loaded_locked()
            record = self._responses.get(source_event_id)
            if record is None:
                return None
            source_event_ids = _source_event_ids_for_record(source_event_id, record)
            return HandledTurnRecord(
                anchor_event_id=_anchor_event_id_for_record(source_event_ids, record),
                source_event_ids=source_event_ids,
                response_event_id=_response_event_id_for_record(record),
                completed=_completed_for_record(record),
                visible_echo_event_id=_visible_echo_event_id_for_record(record),
                source_event_prompts=_prompt_map_for_record(source_event_ids, record),
                source_event_metadata=_source_event_metadata_for_record(source_event_ids, record),
                response_owner=_response_owner_for_record(record),
                requester_id=_requester_id_for_record(record),
                correlation_id=_correlation_id_for_record(record),
                history_scope=_history_scope_for_record(record),
                conversation_target=_conversation_target_for_record(record),
                timestamp=record["timestamp"],
            )

    def _ensure_loaded_locked(self) -> None:
        """Load persisted records into shared memory once while the state lock is held."""
        if self._state.loaded:
            return
        self.base_path.mkdir(parents=True, exist_ok=True)
        with advisory_file_lock(self._responses_lock_file, exclusive=True):
            self._responses = self._read_responses_file_locked()
        self._state.loaded = True

    def _wait_for_pending_persists_locked(self) -> None:
        """Wait for queued disk merges while the state lock is held."""
        pending = list(self._state.pending_persists)
        self._state.pending_persists.clear()
        for future in pending:
            future.result()

    def _schedule_persist_locked(self, source_event_ids: tuple[str, ...]) -> None:
        """Queue one write-behind disk merge for records already applied to memory."""
        records = {event_id: self._responses[event_id] for event_id in source_event_ids}
        future = _persist_executor().submit(self._persist_records, records)
        self._state.pending_persists = [pending for pending in self._state.pending_persists if not pending.done()]
        self._state.pending_persists.append(future)

    def _persist_records(self, records: dict[str, _SerializedHandledTurnRecord]) -> None:
        """Merge already-applied records into the persisted ledger from a worker thread."""
        try:
            with advisory_file_lock(self._responses_lock_file, exclusive=True):
                persisted_responses = self._read_responses_file_locked()
                persisted_responses.update(records)
                self._write_responses_file_locked(persisted_responses)
        except Exception:
            logger.exception(
                "handled_turn_persist_failed",
                agent=self.agent_name,
                responses_file=str(self._responses_file),
            )

    def _write_responses_file_locked(self, responses: dict[str, _SerializedHandledTurnRecord]) -> None:
        """Atomically write one ledger payload while the file lock is held."""
        write_json_file_durable(self._responses_file, responses, temp_dir=self.base_path, indent=2)

    def _cleanup_old_events(self, max_events: int = 10000, max_age_days: int = 30) -> None:
        """Drop stale persisted records by age and count, then reload shared memory."""
        with self._state.lock:
            self._wait_for_pending_persists_locked()
            self.base_path.mkdir(parents=True, exist_ok=True)
            with advisory_file_lock(self._responses_lock_file, exclusive=True):
                self._responses = _cleaned_responses(
                    self._read_responses_file_locked(),
                    max_events=max_events,
                    max_age_days=max_age_days,
                )
                self._write_responses_file_locked(self._responses)
            self._state.loaded = True
        logger.info(
            "handled_turn_cleanup_completed",
            agent=self.agent_name,
            kept_event_count=len(self._responses),
        )

    def _read_responses_file_locked(self) -> dict[str, _SerializedHandledTurnRecord]:
        """Read and normalize persisted responses while the file lock is held."""
        if not self._responses_file.exists():
            return {}
        try:
            with self._responses_file.open(encoding="utf-8") as response_file:
                data = json.load(response_file)
        except (json.JSONDecodeError, UnicodeDecodeError):
            quarantined_file = self._quarantine_corrupt_responses_file_locked()
            logger.warning(
                "Quarantined malformed handled-turn ledger file",
                agent=self.agent_name,
                responses_file=str(self._responses_file),
                quarantined_file=str(quarantined_file or self._responses_file),
            )
            return {}
        if not isinstance(data, dict):
            quarantined_file = self._quarantine_corrupt_responses_file_locked()
            logger.warning(
                "Quarantined structurally invalid handled-turn ledger file",
                agent=self.agent_name,
                responses_file=str(self._responses_file),
                quarantined_file=str(quarantined_file or self._responses_file),
                payload_type=type(data).__name__,
            )
            return {}
        normalized_records: dict[str, _SerializedHandledTurnRecord] = {}
        invalid_event_ids: list[str] = []
        for event_id, record in data.items():
            if not isinstance(event_id, str) or not isinstance(record, dict):
                invalid_event_ids.append(event_id if isinstance(event_id, str) else repr(event_id))
                continue
            normalized_records[event_id] = _normalize_serialized_record(event_id, record)

        if invalid_event_ids:
            quarantined_file = self._quarantine_corrupt_responses_file_locked()
            logger.warning(
                "Quarantined handled-turn ledger file with invalid event entries",
                agent=self.agent_name,
                responses_file=str(self._responses_file),
                quarantined_file=str(quarantined_file or self._responses_file),
                invalid_event_ids=invalid_event_ids,
            )
        return normalized_records

    def _quarantine_corrupt_responses_file_locked(self) -> Path | None:
        """Move a corrupt responses file aside while the file lock is held."""
        quarantined_file = self.base_path / f"{self._responses_file.name}.corrupt-{time.time_ns()}"
        try:
            self._responses_file.replace(quarantined_file)
        except FileNotFoundError:
            return None
        return quarantined_file

    def _visible_echo_for_sources(self, source_event_ids: tuple[str, ...]) -> str | None:
        """Return the first visible echo already tracked for one turn."""
        for event_id in source_event_ids:
            visible_echo_event_id = _visible_echo_event_id_for_record(self._responses.get(event_id))
            if visible_echo_event_id is not None:
                return visible_echo_event_id
        return None

    def _apply_handled_turn_locked(
        self,
        source_event_ids: tuple[str, ...],
        *,
        response_event_id: str | None,
        completed: bool,
        visible_echo_event_id: str | None,
        source_event_prompts: typing.Mapping[str, str] | None,
        source_event_metadata: typing.Mapping[str, SourceEventMetadata] | None,
        response_owner: str | None,
        requester_id: str | None,
        correlation_id: str | None,
        history_scope: HistoryScope | None,
        conversation_target: MessageTarget | None,
        anchor_event_id: str | None = None,
    ) -> None:
        """Apply one handled turn to in-memory state while the state lock is held."""
        visible_echo_event_id = visible_echo_event_id or self._visible_echo_for_sources(source_event_ids)
        prompt_map = self._normalized_prompt_map(source_event_ids, source_event_prompts)
        source_metadata = _explicit_source_event_metadata_for_sources(source_event_ids, source_event_metadata)
        response_owner = self._normalized_response_owner(source_event_ids, response_owner)
        requester_id = self._normalized_requester_id(source_event_ids, requester_id)
        correlation_id = self._normalized_correlation_id(source_event_ids, correlation_id)
        history_scope = self._normalized_history_scope(source_event_ids, history_scope)
        conversation_target = self._normalized_conversation_target(source_event_ids, conversation_target)
        anchor_event_id = self._normalized_anchor_event_id(source_event_ids, anchor_event_id)
        timestamp = time.time()
        for event_id in source_event_ids:
            self._responses[event_id] = _serialized_record(
                timestamp=timestamp,
                response_event_id=response_event_id,
                completed=completed,
                anchor_event_id=anchor_event_id,
                source_event_ids=source_event_ids,
                visible_echo_event_id=visible_echo_event_id,
                source_event_prompts=prompt_map,
                source_event_metadata=source_metadata,
                response_owner=response_owner,
                requester_id=requester_id,
                correlation_id=correlation_id,
                history_scope=history_scope,
                conversation_target=conversation_target,
            )

    def _normalized_prompt_map(
        self,
        source_event_ids: tuple[str, ...],
        source_event_prompts: typing.Mapping[str, str] | None,
    ) -> dict[str, str] | None:
        """Return the explicit prompt map or preserve an existing one."""
        if normalized_prompt_map := _explicit_prompt_map_for_sources(source_event_ids, source_event_prompts):
            return normalized_prompt_map
        for event_id in source_event_ids:
            existing_prompt_map = _prompt_map_for_record(source_event_ids, self._responses.get(event_id))
            if existing_prompt_map is not None:
                return existing_prompt_map
        return None

    def _normalized_response_owner(
        self,
        source_event_ids: tuple[str, ...],
        response_owner: str | None,
    ) -> str | None:
        """Return the explicit response owner or preserve an existing one."""
        normalized_response_owner = _normalized_response_owner(response_owner)
        if normalized_response_owner is not None:
            return normalized_response_owner
        for event_id in source_event_ids:
            existing_response_owner = _response_owner_for_record(self._responses.get(event_id))
            if existing_response_owner is not None:
                return existing_response_owner
        return None

    def _normalized_history_scope(
        self,
        source_event_ids: tuple[str, ...],
        history_scope: HistoryScope | None,
    ) -> HistoryScope | None:
        """Return the explicit history scope or preserve an existing one."""
        normalized_history_scope = _normalized_history_scope(history_scope)
        if normalized_history_scope is not None:
            return normalized_history_scope
        for event_id in source_event_ids:
            existing_history_scope = _history_scope_for_record(self._responses.get(event_id))
            if existing_history_scope is not None:
                return existing_history_scope
        return None

    def _normalized_requester_id(
        self,
        source_event_ids: tuple[str, ...],
        requester_id: str | None,
    ) -> str | None:
        """Return the explicit requester or preserve an existing one."""
        normalized_requester_id = _normalized_requester_id(requester_id)
        if normalized_requester_id is not None:
            return normalized_requester_id
        for event_id in source_event_ids:
            existing_requester_id = _requester_id_for_record(self._responses.get(event_id))
            if existing_requester_id is not None:
                return existing_requester_id
        return None

    def _normalized_correlation_id(
        self,
        source_event_ids: tuple[str, ...],
        correlation_id: str | None,
    ) -> str | None:
        """Return the explicit correlation id or preserve an existing one."""
        normalized_correlation_id = _normalized_correlation_id(correlation_id)
        if normalized_correlation_id is not None:
            return normalized_correlation_id
        for event_id in source_event_ids:
            existing_correlation_id = _correlation_id_for_record(self._responses.get(event_id))
            if existing_correlation_id is not None:
                return existing_correlation_id
        return None

    def _normalized_conversation_target(
        self,
        source_event_ids: tuple[str, ...],
        conversation_target: MessageTarget | None,
    ) -> MessageTarget | None:
        """Return the explicit conversation target or preserve an existing one."""
        normalized_conversation_target = _normalized_conversation_target(conversation_target)
        if normalized_conversation_target is not None:
            return normalized_conversation_target
        for event_id in source_event_ids:
            existing_conversation_target = _conversation_target_for_record(self._responses.get(event_id))
            if existing_conversation_target is not None:
                return existing_conversation_target
        return None

    def _normalized_anchor_event_id(
        self,
        source_event_ids: tuple[str, ...],
        anchor_event_id: str | None,
    ) -> str:
        """Return the explicit anchor event ID or preserve an existing one."""
        normalized_anchor_event_id = _normalized_event_id(anchor_event_id)
        if normalized_anchor_event_id is not None:
            return normalized_anchor_event_id
        for event_id in source_event_ids:
            existing_record = self._responses.get(event_id)
            if existing_record is not None:
                return _anchor_event_id_for_record(source_event_ids, existing_record)
        return source_event_ids[-1]


def _normalize_source_event_ids(source_event_ids: typing.Sequence[str]) -> tuple[str, ...]:
    """Deduplicate source event IDs while preserving order."""
    normalized_event_ids: list[str] = []
    seen_event_ids: set[str] = set()
    for event_id in source_event_ids:
        if not isinstance(event_id, str) or not event_id or event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        normalized_event_ids.append(event_id)
    return tuple(normalized_event_ids)


def _normalized_event_id(event_id: str | None) -> str | None:
    """Return a non-empty Matrix event ID or None."""
    return event_id if isinstance(event_id, str) and event_id else None


def _normalized_response_owner(response_owner: str | None) -> str | None:
    """Return a non-empty response owner or None."""
    return response_owner if isinstance(response_owner, str) and response_owner else None


def _normalized_requester_id(requester_id: str | None) -> str | None:
    """Return a non-empty requester id or None."""
    return requester_id if isinstance(requester_id, str) and requester_id else None


def _normalized_correlation_id(correlation_id: str | None) -> str | None:
    """Return a non-empty correlation id or None."""
    return correlation_id if isinstance(correlation_id, str) and correlation_id else None


def _normalized_history_scope(history_scope: HistoryScope | None) -> HistoryScope | None:
    """Return one normalized persisted history scope."""
    return history_scope if isinstance(history_scope, HistoryScope) else None


def _normalized_conversation_target(conversation_target: MessageTarget | None) -> MessageTarget | None:
    """Return one normalized persisted conversation target."""
    return conversation_target if isinstance(conversation_target, MessageTarget) else None


def _explicit_prompt_map_for_sources(
    source_event_ids: tuple[str, ...],
    source_event_prompts: typing.Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Return only prompt entries that match the tracked source event IDs."""
    if not source_event_prompts:
        return None
    normalized_prompt_map = {
        event_id: prompt
        for event_id in source_event_ids
        if isinstance((prompt := source_event_prompts.get(event_id)), str)
    }
    return normalized_prompt_map or None


def _normalized_source_event_timestamp_ms(raw_timestamp_ms: object) -> float | None:
    """Return a valid Matrix source-event timestamp in milliseconds."""
    if isinstance(raw_timestamp_ms, bool) or not isinstance(raw_timestamp_ms, int | float):
        return None
    timestamp_ms = float(raw_timestamp_ms)
    return timestamp_ms if isfinite(timestamp_ms) and timestamp_ms >= 0 else None


def _explicit_source_event_metadata_for_sources(
    source_event_ids: tuple[str, ...],
    source_event_metadata: typing.Mapping[str, object] | None,
) -> dict[str, SourceEventMetadata] | None:
    """Return source metadata entries that match the tracked source event IDs."""
    if not source_event_metadata:
        return None
    normalized_metadata: dict[str, SourceEventMetadata] = {}
    for event_id in source_event_ids:
        raw_metadata = source_event_metadata.get(event_id)
        metadata = (
            raw_metadata
            if isinstance(raw_metadata, SourceEventMetadata)
            else SourceEventMetadata.from_raw(raw_metadata)
        )
        if metadata is not None:
            normalized_metadata[event_id] = metadata
    return normalized_metadata or None


def _serialized_record(
    *,
    timestamp: float,
    response_event_id: str | None,
    completed: bool,
    anchor_event_id: str | None,
    source_event_ids: tuple[str, ...],
    visible_echo_event_id: str | None = None,
    source_event_prompts: typing.Mapping[str, str] | None = None,
    source_event_metadata: typing.Mapping[str, SourceEventMetadata] | None = None,
    response_owner: str | None = None,
    requester_id: str | None = None,
    correlation_id: str | None = None,
    history_scope: HistoryScope | None = None,
    conversation_target: MessageTarget | None = None,
) -> _SerializedHandledTurnRecord:
    """Build one persisted handled-turn record from normalized fields."""
    record: _SerializedHandledTurnRecord = {
        "timestamp": timestamp,
        "response_event_id": response_event_id,
        "completed": completed,
        "source_event_ids": list(source_event_ids),
    }
    if anchor_event_id is not None and anchor_event_id != source_event_ids[-1]:
        record["anchor_event_id"] = anchor_event_id
    if visible_echo_event_id is not None:
        record["visible_echo_event_id"] = visible_echo_event_id
    if source_event_prompts is not None:
        record["source_event_prompts"] = dict(source_event_prompts)
    if source_event_metadata is not None:
        record["source_event_metadata"] = {
            event_id: metadata.to_record() for event_id, metadata in source_event_metadata.items()
        }
    if response_owner is not None:
        record["response_owner"] = response_owner
    if requester_id is not None:
        record["requester_id"] = requester_id
    if correlation_id is not None:
        record["correlation_id"] = correlation_id
    if history_scope is not None:
        record["history_scope"] = history_scope.to_metadata()
    if conversation_target is not None:
        record["conversation_target"] = conversation_target.to_metadata()
    return record


def _responses_file_path(base_path: Path, agent_name: str) -> Path:
    """Return the validated ledger path for one agent.

    Validation is purely lexical: agent names cannot carry path separators or
    parent references, so the joined path cannot escape ``base_path`` and no
    filesystem access is needed at construction time.
    """
    if not agent_name or ".." in agent_name or "/" in agent_name or "\\" in agent_name:
        message = f"Invalid handled-turn ledger agent name: {agent_name!r}"
        raise ValueError(message)
    return base_path / f"{agent_name}_responded.json"


def _cleaned_responses(
    responses: dict[str, _SerializedHandledTurnRecord],
    *,
    max_events: int,
    max_age_days: int,
) -> dict[str, _SerializedHandledTurnRecord]:
    """Remove stale turn groups while keeping coalesced groups intact."""
    current_time = time.time()
    max_age_seconds = max_age_days * 24 * 60 * 60
    response_groups = _response_groups(responses)
    fresh_groups = [group for group in response_groups if current_time - group.timestamp < max_age_seconds]
    if len(fresh_groups) > max_events:
        fresh_groups = fresh_groups[-max_events:]
    cleaned_responses: dict[str, _SerializedHandledTurnRecord] = {}
    for group in fresh_groups:
        cleaned_responses.update(group.records)
    return cleaned_responses


@dataclass(frozen=True)
class _ResponseGroup:
    """Logical handled-turn group keyed by coalesced source IDs."""

    source_event_ids: tuple[str, ...]
    timestamp: float
    records: dict[str, _SerializedHandledTurnRecord]


def _response_groups(
    responses: dict[str, _SerializedHandledTurnRecord],
) -> list[_ResponseGroup]:
    """Return handled turns grouped by shared source-event identity."""
    grouped_records: dict[tuple[str, ...], dict[str, _SerializedHandledTurnRecord]] = {}
    grouped_timestamps: dict[tuple[str, ...], float] = {}
    for event_id, record in responses.items():
        source_event_ids = _source_event_ids_for_record(event_id, record)
        grouped_records.setdefault(source_event_ids, {})[event_id] = record
        grouped_timestamps[source_event_ids] = max(grouped_timestamps.get(source_event_ids, 0.0), record["timestamp"])
    return sorted(
        (
            _ResponseGroup(
                source_event_ids=source_event_ids,
                timestamp=grouped_timestamps[source_event_ids],
                records=records,
            )
            for source_event_ids, records in grouped_records.items()
        ),
        key=lambda group: group.timestamp,
    )


def _normalize_serialized_record(
    event_id: str,
    raw_record: _SerializedHandledTurnRecordLike,
) -> _SerializedHandledTurnRecord:
    """Normalize one on-disk record into the current schema."""
    response_event_id = raw_record.get("response_event_id")
    visible_echo_event_id = raw_record.get("visible_echo_event_id")
    timestamp = raw_record.get("timestamp")
    raw_source_event_ids = raw_record.get("source_event_ids")
    normalized_source_event_ids = (
        _normalize_source_event_ids(raw_source_event_ids)
        if isinstance(
            raw_source_event_ids,
            list,
        )
        else (event_id,)
    )
    if not normalized_source_event_ids:
        normalized_source_event_ids = (event_id,)
    anchor_event_id = _normalized_event_id(raw_record.get("anchor_event_id"))
    prompt_map = _prompt_map_for_record(normalized_source_event_ids, raw_record)
    source_metadata = _source_event_metadata_for_record(normalized_source_event_ids, raw_record)
    response_owner = _response_owner_for_record(raw_record)
    requester_id = _requester_id_for_record(raw_record)
    correlation_id = _correlation_id_for_record(raw_record)
    history_scope = _history_scope_for_record(raw_record)
    conversation_target = _conversation_target_for_record(raw_record)
    return _serialized_record(
        timestamp=float(timestamp) if isinstance(timestamp, int | float) else 0.0,
        response_event_id=response_event_id if isinstance(response_event_id, str) else None,
        completed=bool(raw_record.get("completed", True)),
        anchor_event_id=anchor_event_id,
        source_event_ids=normalized_source_event_ids,
        visible_echo_event_id=visible_echo_event_id if isinstance(visible_echo_event_id, str) else None,
        source_event_prompts=prompt_map,
        source_event_metadata=source_metadata,
        response_owner=response_owner,
        requester_id=requester_id,
        correlation_id=correlation_id,
        history_scope=history_scope,
        conversation_target=conversation_target,
    )


def _source_event_ids_for_record(
    event_id: str,
    record: _SerializedHandledTurnRecordLike | None,
) -> tuple[str, ...]:
    """Return the normalized source event IDs for one record."""
    if record is None:
        return (event_id,)
    raw_source_event_ids = record.get("source_event_ids")
    if isinstance(raw_source_event_ids, list):
        normalized_source_event_ids = _normalize_source_event_ids(raw_source_event_ids)
        if normalized_source_event_ids:
            return normalized_source_event_ids
    return (event_id,)


def _prompt_map_for_record(
    source_event_ids: tuple[str, ...],
    record: _SerializedHandledTurnRecordLike | None,
) -> dict[str, str] | None:
    """Return the prompt map for one record if present."""
    if record is None:
        return None
    raw_prompt_map = record.get("source_event_prompts")
    if not isinstance(raw_prompt_map, dict):
        return None
    normalized_prompt_map = {
        event_id: prompt for event_id in source_event_ids if isinstance((prompt := raw_prompt_map.get(event_id)), str)
    }
    return normalized_prompt_map or None


def _source_event_metadata_for_record(
    source_event_ids: tuple[str, ...],
    record: _SerializedHandledTurnRecordLike | None,
) -> dict[str, SourceEventMetadata] | None:
    """Return source metadata for one record if present."""
    if record is None:
        return None
    raw_source_event_metadata = record.get("source_event_metadata")
    if not isinstance(raw_source_event_metadata, Mapping):
        return None
    return _explicit_source_event_metadata_for_sources(source_event_ids, raw_source_event_metadata)


def _anchor_event_id_for_record(
    source_event_ids: tuple[str, ...],
    record: _SerializedHandledTurnRecordLike | None,
) -> str:
    """Return the normalized anchor event ID for one record."""
    if record is None:
        return source_event_ids[-1]
    anchor_event_id = _normalized_event_id(record.get("anchor_event_id"))
    return anchor_event_id if anchor_event_id is not None else source_event_ids[-1]


def _response_owner_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized response owner for one record."""
    if record is None:
        return None
    return _normalized_response_owner(record.get("response_owner"))


def _requester_id_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized requester id for one record."""
    if record is None:
        return None
    return _normalized_requester_id(record.get("requester_id"))


def _correlation_id_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized correlation id for one record."""
    if record is None:
        return None
    return _normalized_correlation_id(record.get("correlation_id"))


def _history_scope_for_record(record: _SerializedHandledTurnRecordLike | None) -> HistoryScope | None:
    """Return the normalized history scope for one record."""
    if record is None:
        return None
    return HistoryScope.from_metadata(record.get("history_scope"))


def _conversation_target_for_record(record: _SerializedHandledTurnRecordLike | None) -> MessageTarget | None:
    """Return the normalized conversation target for one record."""
    if record is None:
        return None
    return MessageTarget.from_metadata(record.get("conversation_target"))


def _response_event_id_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized response event ID for one record."""
    if record is None:
        return None
    response_event_id = record.get("response_event_id")
    return response_event_id if isinstance(response_event_id, str) else None


def _visible_echo_event_id_for_record(record: _SerializedHandledTurnRecordLike | None) -> str | None:
    """Return the normalized visible echo event ID for one record."""
    if record is None:
        return None
    visible_echo_event_id = record.get("visible_echo_event_id")
    return visible_echo_event_id if isinstance(visible_echo_event_id, str) else None


def _completed_for_record(record: _SerializedHandledTurnRecordLike | None) -> bool:
    """Return the normalized terminal-completion flag for one record."""
    return bool(record.get("completed", True)) if record is not None else False
