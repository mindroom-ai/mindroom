"""Persist canonical turn records for one runtime entity.

Reads are served from in-memory state shared across every ledger bound to the
same responses file, so sibling ledger instances in one process observe each
other's writes without touching the filesystem. Disk persistence uses one
ordered drain per ledger on a bounded process-wide worker pool. One runtime
process owns semantic ordering; an advisory lock keeps file updates atomic
without blocking the event loop on filesystem I/O (issue #1260).
"""

from __future__ import annotations

import json
import threading
import time
import typing
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from mindroom import constants
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.history.types import HistoryScope
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.turn_record import (
    SourceEventMetadata,
    SourceEventRevision,
    TurnRecord,
    canonical_optional_string,
    canonical_source_event_ids,
    canonicalize_turn_record,
    merge_edit_facts,
    same_turn_identity,
)

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from pathlib import Path

logger = get_logger(__name__)

__all__ = [
    "HandledTurnLedger",
    "SourceEventMetadata",
    "SourceEventRevision",
    "TurnRecord",
    "TurnRecordCodec",
    "canonicalize_turn_record",
    "merge_edit_facts",
    "with_user_stop",
]

_TURN_RECORD_SCHEMA_VERSION = 1
_LEDGER_SCHEMA_VERSION_KEY = "schema_version"
_LEDGER_RECORDS_KEY = "records"
# Let independent agent ledgers make progress without allowing unbounded
# concurrent durable writes and fsync pressure.
_PERSIST_EXECUTOR_MAX_WORKERS = 8
_PERSIST_RETRY_INITIAL_DELAY_SECONDS = 0.05
_PERSIST_RETRY_MAX_DELAY_SECONDS = 5.0


def with_user_stop(
    turn_record: TurnRecord,
    response_event_id: str,
    stop_receipt_order: int,
    *,
    delivery_settled: bool = False,
) -> TurnRecord:
    """Return the monotonic durable state for one admitted STOP callback."""
    if isinstance(stop_receipt_order, bool) or stop_receipt_order <= 0:
        msg = "User-stop receipt order must be positive"
        raise ValueError(msg)
    return canonicalize_turn_record(
        turn_record,
        response_event_id=response_event_id,
        completed=True,
        user_stop_receipt_order=max(
            stop_receipt_order,
            turn_record.user_stop_receipt_order or stop_receipt_order,
        ),
        user_stop_settled_receipt_order=max(
            turn_record.user_stop_settled_receipt_order or 0,
            stop_receipt_order if delivery_settled else 0,
        )
        or None,
        timestamp=0.0,
    )


class TurnRecordCodec:
    """Encode the canonical record into its two intentional physical projections."""

    @staticmethod
    def schema_version() -> int:
        """Return the persisted schema version emitted by this codec."""
        return _TURN_RECORD_SCHEMA_VERSION

    @staticmethod
    def _to_ledger_record(record: TurnRecord) -> dict[str, object]:  # noqa: C901, PLR0912
        """Serialize one exact record for the versioned handled-turn ledger."""
        payload: dict[str, object] = {
            "anchor_event_id": record.anchor_event_id,
            "source_event_ids": list(record.source_event_ids),
            "redacted_source_event_ids": list(record.redacted_source_event_ids),
            "pending_redaction_cleanup_event_ids": list(record.pending_redaction_cleanup_event_ids),
            "response_event_id": record.response_event_id,
            "completed": record.completed,
            "timestamp": record.timestamp,
        }
        if record.discovery_event_ids:
            payload["discovery_event_ids"] = list(record.discovery_event_ids)
        if record.visible_echo_event_id is not None:
            payload["visible_echo_event_id"] = record.visible_echo_event_id
        if record.visible_echo_is_fallback is not None:
            payload["visible_echo_is_fallback"] = record.visible_echo_is_fallback
        if record.source_event_prompts is not None:
            payload["source_event_prompts"] = dict(record.source_event_prompts)
        if record.source_event_revisions is not None:
            payload["source_event_revisions"] = {
                event_id: list(revision) for event_id, revision in record.source_event_revisions.items()
            }
        if record.suppressed_source_event_revisions is not None:
            payload["suppressed_source_event_revisions"] = {
                event_id: list(revision) for event_id, revision in record.suppressed_source_event_revisions.items()
            }
        if record.latest_edit_receipt_order is not None:
            payload["latest_edit_receipt_order"] = record.latest_edit_receipt_order
        if record.user_stop_receipt_order is not None:
            payload["user_stop_receipt_order"] = record.user_stop_receipt_order
        if record.user_stop_settled_receipt_order is not None:
            payload["user_stop_settled_receipt_order"] = record.user_stop_settled_receipt_order
        if record.source_event_metadata is not None:
            payload["source_event_metadata"] = {
                event_id: metadata._to_record() for event_id, metadata in record.source_event_metadata.items()
            }
        if record.response_owner is not None:
            payload["response_owner"] = record.response_owner
        if record.requester_id is not None:
            payload["requester_id"] = record.requester_id
        if record.correlation_id is not None:
            payload["correlation_id"] = record.correlation_id
        if record.command_execution_started:
            payload["command_execution_started"] = True
        if record.command_result_text is not None:
            payload["command_result_text"] = record.command_result_text
        if record.history_scope is not None:
            payload["history_scope"] = record.history_scope.to_metadata()
        if record.conversation_target is not None:
            payload["conversation_target"] = record.conversation_target.to_metadata()
        return payload

    @staticmethod
    def _from_ledger_record(event_id: str, raw_record: object) -> TurnRecord | None:
        """Parse one record from the current ledger schema without legacy migration."""
        if not isinstance(raw_record, Mapping):
            return None
        record = typing.cast("Mapping[str, object]", raw_record)
        raw_source_event_ids = record.get("source_event_ids")
        raw_discovery_event_ids = record.get("discovery_event_ids", [])
        raw_redacted_source_event_ids = record.get("redacted_source_event_ids", [])
        raw_pending_redaction_cleanup_event_ids = record.get("pending_redaction_cleanup_event_ids", [])
        anchor_event_id = record.get("anchor_event_id")
        completed = record.get("completed")
        timestamp = record.get("timestamp")
        response_event_id = record.get("response_event_id")
        if (
            not isinstance(raw_source_event_ids, list)
            or not isinstance(raw_discovery_event_ids, list)
            or not isinstance(raw_redacted_source_event_ids, list)
            or not isinstance(raw_pending_redaction_cleanup_event_ids, list)
            or not isinstance(anchor_event_id, str)
            or not anchor_event_id
            or not isinstance(completed, bool)
            or not isinstance(timestamp, int | float)
            or isinstance(timestamp, bool)
            or (response_event_id is not None and not isinstance(response_event_id, str))
        ):
            return None
        source_event_ids = canonical_source_event_ids(raw_source_event_ids)
        if not source_event_ids:
            return None
        turn_record = TurnRecord.create(
            source_event_ids,
            discovery_event_ids=canonical_source_event_ids(raw_discovery_event_ids),
            redacted_source_event_ids=canonical_source_event_ids(raw_redacted_source_event_ids),
            pending_redaction_cleanup_event_ids=canonical_source_event_ids(
                raw_pending_redaction_cleanup_event_ids,
            ),
            anchor_event_id=anchor_event_id,
            response_event_id=response_event_id,
            completed=completed,
            visible_echo_event_id=canonical_optional_string(record.get("visible_echo_event_id")),
            visible_echo_is_fallback=_bool_or_none(record.get("visible_echo_is_fallback")),
            source_event_prompts=_mapping_or_none(record.get("source_event_prompts")),
            source_event_revisions=_mapping_or_none(record.get("source_event_revisions")),
            suppressed_source_event_revisions=_mapping_or_none(
                record.get("suppressed_source_event_revisions"),
            ),
            latest_edit_receipt_order=_positive_int_or_none(record.get("latest_edit_receipt_order")),
            user_stop_receipt_order=_positive_int_or_none(record.get("user_stop_receipt_order")),
            user_stop_settled_receipt_order=_positive_int_or_none(
                record.get("user_stop_settled_receipt_order"),
            ),
            source_event_metadata=_mapping_or_none(record.get("source_event_metadata")),
            response_owner=canonical_optional_string(record.get("response_owner")),
            requester_id=canonical_optional_string(record.get("requester_id")),
            correlation_id=canonical_optional_string(record.get("correlation_id")),
            command_execution_started=record.get("command_execution_started") is True,
            command_result_text=canonical_optional_string(record.get("command_result_text")),
            history_scope=HistoryScope.from_metadata(record.get("history_scope")),
            conversation_target=MessageTarget.from_metadata(record.get("conversation_target")),
            timestamp=float(timestamp),
        )
        if event_id not in turn_record.indexed_event_ids:
            return None
        return turn_record

    @staticmethod
    def to_run_metadata(record: TurnRecord) -> dict[str, object]:  # noqa: C901
        """Project one record into the recoverable subset stored with an Agno run."""
        if not record.source_event_ids:
            return {}
        metadata: dict[str, object] = {
            constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY: TurnRecordCodec.schema_version(),
            constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: list(record.source_event_ids),
        }
        if record.discovery_event_ids:
            metadata[constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY] = list(record.discovery_event_ids)
        if record.redacted_source_event_ids:
            metadata[constants.MATRIX_TURN_REDACTED_SOURCE_EVENT_IDS_METADATA_KEY] = list(
                record.redacted_source_event_ids,
            )
        if record.source_event_prompts is not None:
            metadata[constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY] = dict(record.source_event_prompts)
        if record.source_event_revisions is not None:
            metadata[constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY] = {
                event_id: list(revision) for event_id, revision in record.source_event_revisions.items()
            }
        if record.source_event_metadata is not None:
            metadata[constants.MATRIX_SOURCE_EVENT_METADATA_KEY] = {
                event_id: source_metadata._to_record()
                for event_id, source_metadata in record.source_event_metadata.items()
            }
        if record.response_owner is not None:
            metadata[constants.MATRIX_RESPONSE_OWNER_METADATA_KEY] = record.response_owner
        if record.requester_id is not None:
            metadata["requester_id"] = record.requester_id
        if record.history_scope is not None:
            metadata[constants.MATRIX_HISTORY_SCOPE_METADATA_KEY] = record.history_scope.to_metadata()
        if record.conversation_target is not None:
            metadata[constants.MATRIX_CONVERSATION_TARGET_METADATA_KEY] = record.conversation_target.to_metadata()
        return metadata

    @staticmethod
    def from_run_metadata(metadata: Mapping[str, object]) -> TurnRecord | None:
        """Parse current Agno metadata, using response linkage as terminal-delivery evidence."""
        if metadata.get(constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY) != TurnRecordCodec.schema_version():
            return None
        anchor_event_id = metadata.get(constants.MATRIX_EVENT_ID_METADATA_KEY)
        if not isinstance(anchor_event_id, str) or not anchor_event_id:
            return None
        raw_source_event_ids = metadata.get(constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)
        raw_discovery_event_ids = metadata.get(constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY)
        raw_redacted_source_event_ids = metadata.get(
            constants.MATRIX_TURN_REDACTED_SOURCE_EVENT_IDS_METADATA_KEY,
        )
        source_event_ids = (
            canonical_source_event_ids(raw_source_event_ids)
            if isinstance(raw_source_event_ids, list)
            else (anchor_event_id,)
        ) or (anchor_event_id,)
        response_event_id = canonical_optional_string(metadata.get(constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY))
        return TurnRecord.create(
            source_event_ids,
            discovery_event_ids=(
                canonical_source_event_ids(raw_discovery_event_ids) if isinstance(raw_discovery_event_ids, list) else ()
            ),
            redacted_source_event_ids=(
                canonical_source_event_ids(raw_redacted_source_event_ids)
                if isinstance(raw_redacted_source_event_ids, list)
                else ()
            ),
            anchor_event_id=anchor_event_id,
            response_event_id=response_event_id,
            completed=response_event_id is not None,
            source_event_prompts=_mapping_or_none(metadata.get(constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY)),
            source_event_revisions=_mapping_or_none(
                metadata.get(constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY),
            ),
            source_event_metadata=_mapping_or_none(metadata.get(constants.MATRIX_SOURCE_EVENT_METADATA_KEY)),
            response_owner=canonical_optional_string(metadata.get(constants.MATRIX_RESPONSE_OWNER_METADATA_KEY)),
            requester_id=canonical_optional_string(metadata.get("requester_id")),
            correlation_id=canonical_optional_string(metadata.get("correlation_id")),
            history_scope=HistoryScope.from_metadata(metadata.get(constants.MATRIX_HISTORY_SCOPE_METADATA_KEY)),
            conversation_target=MessageTarget.from_metadata(
                metadata.get(constants.MATRIX_CONVERSATION_TARGET_METADATA_KEY),
            ),
        )


@dataclass
class _PersistRequest:
    """One ordered ledger persist request and its exact durability waiter."""

    records: tuple[TurnRecord, ...]
    completion: Future[None] | None
    on_persisted: Callable[[], None] | None = None


@dataclass
class _LedgerState:
    """In-memory canonical records shared by every ledger bound to one file."""

    responses: dict[str, TurnRecord] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    loaded: bool = False
    persist_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    pending_persists: list[_PersistRequest] = field(default_factory=list, repr=False)
    persist_active: bool = False
    persist_retry_timer: threading.Timer | None = field(default=None, repr=False)
    persist_retry_delay_seconds: float = _PERSIST_RETRY_INITIAL_DELAY_SECONDS
    shutting_down: bool = False


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
    """Return a bounded shared executor; each ledger still schedules only one drain."""
    global _PERSIST_EXECUTOR
    with _LEDGER_RUNTIME_LOCK:
        if _PERSIST_EXECUTOR is None:
            _PERSIST_EXECUTOR = ThreadPoolExecutor(
                max_workers=_PERSIST_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="handled-turn-persist",
            )
        return _PERSIST_EXECUTOR


def _reset_handled_turn_ledger_runtime() -> None:
    """Flush pending persists and drop shared ledger state (tests and forked runtimes)."""
    global _PERSIST_EXECUTOR
    with _LEDGER_RUNTIME_LOCK:
        executor = _PERSIST_EXECUTOR
        _PERSIST_EXECUTOR = None
        states = tuple(_LEDGER_STATES.values())
        _LEDGER_STATES.clear()
    for state in states:
        with state.persist_lock:
            state.shutting_down = True
            if state.persist_retry_timer is not None:
                state.persist_retry_timer.cancel()
                state.persist_retry_timer = None
    if executor is not None:
        executor.shutdown(wait=True)


@dataclass
class HandledTurnLedger:
    """Store exact canonical records without reassigning completed source identities."""

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
    def _responses(self) -> dict[str, TurnRecord]:
        return self._state.responses

    @_responses.setter
    def _responses(self, responses: dict[str, TurnRecord]) -> None:
        self._state.responses = responses

    def load(self) -> None:
        """Load persisted truth without pruning records needed by later recovery."""
        with self._state.lock:
            self._wait_for_pending_persists_locked()
            self._ensure_loaded_locked()

    def cleanup(self, *, unsettled_source_event_ids: Collection[str] = ()) -> None:
        """Compact terminal history while retaining truth still owned by dispatch."""
        self._cleanup_old_events(unsettled_source_event_ids=unsettled_source_event_ids)

    def flush(self) -> None:
        """Block until every scheduled persist completes, propagating write failures."""
        with self._state.lock:
            self._wait_for_pending_persists_locked()

    def record_handled_turn(self, turn_record: TurnRecord) -> None:
        """Persist one exact record for every source event in the turn."""
        self.update_handled_turn(
            turn_record.indexed_event_ids,
            lambda _existing_records: turn_record,
        )

    def update_handled_turn(
        self,
        lookup_event_ids: Sequence[str],
        update: Callable[[Mapping[str, TurnRecord]], TurnRecord],
        *,
        wait_for_persist: bool = False,
        on_persisted: Callable[[TurnRecord], None] | None = None,
    ) -> TurnRecord | None:
        """Atomically update one record, optionally waiting for its exact persist."""
        normalized_lookup_event_ids = canonical_source_event_ids(lookup_event_ids)
        if not normalized_lookup_event_ids:
            return None
        with self._state.lock:
            self._ensure_loaded_locked()
            existing_records = MappingProxyType(
                {
                    event_id: record
                    for event_id in normalized_lookup_event_ids
                    if (record := self._responses.get(event_id)) is not None
                },
            )
            updated_record = update(existing_records)
            candidate_record = canonicalize_turn_record(
                updated_record,
                timestamp=(updated_record.timestamp if updated_record.timestamp != 0.0 else time.time()),
            )
            if not candidate_record.source_event_ids:
                return None
            persisted_record = _resolve_turn_record(candidate_record, self._responses)
            if persisted_record is None:
                return None
            for event_id in persisted_record.indexed_event_ids:
                self._responses[event_id] = persisted_record
            persist_future = self._schedule_persist_locked(
                persisted_record,
                on_persisted=on_persisted,
            )
        if wait_for_persist:
            persist_future.result()
        logger.debug("handled_turn_recorded", indexed_event_count=len(persisted_record.indexed_event_ids))
        return persisted_record

    def has_responded(self, event_id: str) -> bool:
        """Return whether the source event has a terminal recorded outcome."""
        with self._state.lock:
            self._ensure_loaded_locked()
            return self._has_responded_locked(event_id)

    def has_durably_responded(self, event_id: str) -> bool:
        """Return terminal truth only after all preceding ledger writes reach disk."""
        with self._state.lock:
            self._ensure_loaded_locked()
            if not self._has_responded_locked(event_id):
                return False
            barrier = self._schedule_persist_barrier_locked()
        barrier.result()
        with self._state.lock:
            return self._has_responded_locked(event_id)

    def _has_responded_locked(self, event_id: str) -> bool:
        record = self._responses.get(event_id)
        if record is None:
            return False
        return record.completed or event_id in record.redacted_source_event_ids

    def get_visible_echo_event_id(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo event ID for one source event."""
        with self._state.lock:
            self._ensure_loaded_locked()
            record = self._responses.get(source_event_id)
            return record.visible_echo_event_id if record is not None else None

    def get_turn_record(self, source_event_id: str) -> TurnRecord | None:
        """Return the canonical record for one source event."""
        with self._state.lock:
            self._ensure_loaded_locked()
            return self._responses.get(source_event_id)

    def pending_redaction_cleanup_event_ids(self) -> tuple[str, ...]:
        """Return every durable redaction cleanup intent still awaiting completion."""
        with self._state.lock:
            self._ensure_loaded_locked()
            return canonical_source_event_ids(
                tuple(
                    event_id
                    for record in self._responses.values()
                    for event_id in record.pending_redaction_cleanup_event_ids
                ),
            )

    def turn_records_for_conversation(
        self,
        *,
        session_id: str,
    ) -> tuple[TurnRecord, ...]:
        """Return unique records that can identify persisted scopes for one conversation."""
        with self._state.lock:
            self._ensure_loaded_locked()
            unique_records: dict[tuple[str, ...], TurnRecord] = {}
            for record in self._responses.values():
                target = record.conversation_target
                if target is None or target.session_id != session_id:
                    continue
                unique_records[record.indexed_event_ids] = record
            return tuple(unique_records.values())

    def turn_record_for_response_event_id(self, response_event_id: str) -> TurnRecord | None:
        """Return the sole turn whose visible response has this Matrix event ID."""
        with self._state.lock:
            self._ensure_loaded_locked()
            matches = {
                record.indexed_event_ids: record
                for record in self._responses.values()
                if response_event_id in {record.response_event_id, record.visible_echo_event_id}
            }
        if len(matches) > 1:
            msg = f"Multiple turns own visible response {response_event_id!r}"
            raise RuntimeError(msg)
        return next(iter(matches.values()), None)

    def _ensure_loaded_locked(self) -> None:
        """Load persisted records into shared memory once while the state lock is held."""
        if self._state.loaded:
            return
        self.base_path.mkdir(parents=True, exist_ok=True)
        with advisory_file_lock(self._responses_lock_file, exclusive=True):
            self._responses = self._read_responses_file_locked()
        self._state.loaded = True

    def _wait_for_pending_persists_locked(self) -> None:
        """Wait for the exact FIFO prefix queued before this barrier."""
        self._schedule_persist_barrier_locked().result()

    def _schedule_persist_barrier_locked(self) -> Future[None]:
        """Queue one exact FIFO durability barrier while state mutation is excluded."""
        barrier: Future[None] = Future()
        with self._state.persist_lock:
            self._state.pending_persists.append(_PersistRequest(records=(), completion=barrier))
            self._ensure_persist_drain_locked()
        return barrier

    def _schedule_persist_locked(
        self,
        turn_record: TurnRecord,
        *,
        on_persisted: Callable[[TurnRecord], None] | None = None,
    ) -> Future[None]:
        """Queue one write-behind disk merge for records already applied to memory."""
        completion: Future[None] = Future()
        with self._state.persist_lock:
            self._state.pending_persists.append(
                _PersistRequest(
                    records=(turn_record,),
                    completion=completion,
                    on_persisted=(lambda: on_persisted(turn_record)) if on_persisted is not None else None,
                ),
            )
            self._ensure_persist_drain_locked()
        return completion

    def _ensure_persist_drain_locked(self) -> None:
        """Start this ledger's sole drain while ``persist_lock`` is held."""
        if self._state.persist_active or self._state.shutting_down:
            return
        self._state.persist_active = True
        try:
            _persist_executor().submit(self._persist_pending_records)
        except Exception:
            self._state.persist_active = False
            raise

    def _persist_pending_records(self) -> None:
        """Drain FIFO batches, retrying one failed batch without failing later traffic."""
        retry_available = True
        while True:
            with self._state.persist_lock:
                if not self._state.pending_persists:
                    self._state.persist_active = False
                    self._reset_persist_retry_locked()
                    return
                requests = tuple(self._state.pending_persists)
                self._state.pending_persists.clear()
                # Nothing but already-failed records left to retry: stop rather
                # than spin against a disk that is still refusing writes.
                if not retry_available and all(request.completion is None for request in requests):
                    self._state.pending_persists[0:0] = requests
                    self._state.persist_active = False
                    self._schedule_persist_retry_locked()
                    return
            records = tuple(record for request in requests for record in request.records)
            try:
                if records:
                    self._persist_records(records)
            except Exception as exc:
                self._requeue_failed_persist_batch(
                    requests,
                    exc,
                    retry_available=retry_available,
                )
                # One retry per failure: the requeued batch is attempted once more,
                # and if that also fails its waiters are failed instead of looping.
                retry_available = False
                continue
            for request in requests:
                if request.completion is not None and not request.completion.done():
                    request.completion.set_result(None)
                if request.on_persisted is not None:
                    try:
                        request.on_persisted()
                    except Exception:
                        logger.exception("handled_turn_persist_notification_failed", agent=self.agent_name)
            retry_available = True

    def _schedule_persist_retry_locked(self) -> None:
        """Schedule one delayed autonomous retry without occupying a persist worker."""
        if self._state.shutting_down:
            return
        existing_retry_timer = self._state.persist_retry_timer
        if existing_retry_timer is not None and existing_retry_timer.is_alive():
            return
        delay_seconds = self._state.persist_retry_delay_seconds

        def retry_pending() -> None:
            self._retry_pending_persists(scheduled_retry_timer)

        scheduled_retry_timer = threading.Timer(delay_seconds, retry_pending)
        scheduled_retry_timer.daemon = True
        self._state.persist_retry_timer = scheduled_retry_timer
        self._state.persist_retry_delay_seconds = min(
            delay_seconds * 2,
            _PERSIST_RETRY_MAX_DELAY_SECONDS,
        )
        scheduled_retry_timer.start()

    def _retry_pending_persists(self, retry_timer: threading.Timer) -> None:
        """Return delayed failed records to their ledger's sole drain."""
        with self._state.persist_lock:
            if self._state.persist_retry_timer is not retry_timer:
                return
            self._state.persist_retry_timer = None
            self._ensure_persist_drain_locked()

    def _reset_persist_retry_locked(self) -> None:
        """Reset retry backoff after the pending queue drains successfully."""
        retry_timer = self._state.persist_retry_timer
        if retry_timer is not None:
            retry_timer.cancel()
            self._state.persist_retry_timer = None
        self._state.persist_retry_delay_seconds = _PERSIST_RETRY_INITIAL_DELAY_SECONDS

    def _requeue_failed_persist_batch(
        self,
        requests: tuple[_PersistRequest, ...],
        error: Exception,
        *,
        retry_available: bool,
    ) -> None:
        """Requeue the failed batch, failing only the waiters it actually attempted.

        While a retry is still available the batch is requeued intact so the next
        attempt can still satisfy its waiters. Once exhausted, the records are kept
        for a later drain but their waiters are failed. Requests queued *after* this
        batch were never written, so they always keep their completions: failing them
        would report a durability error for a write that was never attempted.
        """
        with self._state.persist_lock:
            if retry_available:
                self._state.pending_persists[0:0] = requests
                return
            # Keep records and post-persist notifications for autonomous retry,
            # but drop waiters that are about to receive this bounded failure.
            retry_requests = tuple(
                _PersistRequest(
                    records=request.records,
                    completion=None,
                    on_persisted=request.on_persisted,
                )
                for request in requests
                if request.records
            )
            self._state.pending_persists[0:0] = retry_requests
        attempted_completions = tuple(
            request.completion
            for request in requests
            if request.completion is not None and not request.completion.done()
        )
        for completion in attempted_completions:
            completion.set_exception(error)

    def _persist_records(self, turn_records: tuple[TurnRecord, ...]) -> None:
        """Merge one batch of already-applied records from the persistence worker."""
        try:
            with advisory_file_lock(self._responses_lock_file, exclusive=True):
                persisted_responses = self._read_responses_file_locked()
                for turn_record in turn_records:
                    for event_id in turn_record.indexed_event_ids:
                        persisted_responses[event_id] = turn_record
                self._write_responses_file_locked(persisted_responses)
        except Exception:
            logger.exception(
                "handled_turn_persist_failed",
                agent=self.agent_name,
                responses_file=str(self._responses_file),
                batch_size=len(turn_records),
            )
            raise

    def _write_responses_file_locked(self, responses: dict[str, TurnRecord]) -> None:
        """Atomically write one versioned ledger payload while the file lock is held."""
        payload = {
            _LEDGER_SCHEMA_VERSION_KEY: TurnRecordCodec.schema_version(),
            _LEDGER_RECORDS_KEY: {
                event_id: TurnRecordCodec._to_ledger_record(record) for event_id, record in responses.items()
            },
        }
        write_json_file_durable(self._responses_file, payload, temp_dir=self.base_path, indent=2)

    def _cleanup_old_events(
        self,
        max_events: int = 10000,
        max_age_days: int = 30,
        *,
        unsettled_source_event_ids: Collection[str] = (),
    ) -> None:
        """Drop stale persisted records by age and count, then reload shared memory."""
        with self._state.lock:
            self._wait_for_pending_persists_locked()
            self.base_path.mkdir(parents=True, exist_ok=True)
            with advisory_file_lock(self._responses_lock_file, exclusive=True):
                self._responses = _cleaned_responses(
                    self._read_responses_file_locked(),
                    max_events=max_events,
                    max_age_days=max_age_days,
                    unsettled_source_event_ids=unsettled_source_event_ids,
                )
                self._write_responses_file_locked(self._responses)
            self._state.loaded = True
        logger.info(
            "handled_turn_cleanup_completed",
            agent=self.agent_name,
            kept_event_count=len(self._responses),
        )

    def _read_responses_file_locked(self) -> dict[str, TurnRecord]:
        """Read current-version canonical records while the file lock is held."""
        if not self._responses_file.exists():
            return {}
        try:
            with self._responses_file.open(encoding="utf-8") as response_file:
                data = json.load(response_file)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._quarantine_with_warning("malformed")
            return {}
        if not isinstance(data, dict):
            self._quarantine_with_warning("structurally invalid", payload_type=type(data).__name__)
            return {}
        if data.get(_LEDGER_SCHEMA_VERSION_KEY) != TurnRecordCodec.schema_version():
            self._quarantine_with_warning("unsupported-schema")
            return {}
        raw_records = data.get(_LEDGER_RECORDS_KEY)
        if not isinstance(raw_records, dict):
            self._quarantine_with_warning("structurally invalid records")
            return {}
        records: dict[str, TurnRecord] = {}
        invalid_event_ids: list[str] = []
        for event_id, raw_record in raw_records.items():
            record = TurnRecordCodec._from_ledger_record(event_id, raw_record) if isinstance(event_id, str) else None
            if record is None:
                invalid_event_ids.append(event_id if isinstance(event_id, str) else repr(event_id))
                continue
            records[event_id] = record
        rehydrated_records = {event_id: record for record in records.values() for event_id in record.indexed_event_ids}
        rehydrated_records.update(records)
        records = rehydrated_records
        if invalid_event_ids and not records:
            self._quarantine_with_warning("invalid event entries", invalid_event_ids=invalid_event_ids)
        elif invalid_event_ids:
            logger.warning(
                "Ignored invalid handled-turn ledger entries",
                agent=self.agent_name,
                responses_file=str(self._responses_file),
                invalid_event_ids=invalid_event_ids,
            )
        return records

    def _quarantine_with_warning(self, reason: str, **context: object) -> None:
        """Quarantine an unreadable ledger and log why its current schema was rejected."""
        quarantined_file = self._quarantine_corrupt_responses_file_locked()
        logger.warning(
            "Quarantined handled-turn ledger file",
            reason=reason,
            agent=self.agent_name,
            responses_file=str(self._responses_file),
            quarantined_file=str(quarantined_file or self._responses_file),
            **context,
        )

    def _quarantine_corrupt_responses_file_locked(self) -> Path | None:
        """Move a corrupt responses file aside while the file lock is held."""
        quarantined_file = self.base_path / f"{self._responses_file.name}.corrupt-{time.time_ns()}"
        try:
            self._responses_file.replace(quarantined_file)
        except FileNotFoundError:
            return None
        return quarantined_file


def _resolve_turn_record(
    turn_record: TurnRecord,
    existing_records: Mapping[str, TurnRecord],
) -> TurnRecord | None:
    """Resolve one candidate against completed identities and newer same-turn rows."""
    conflicting_source_event_ids = tuple(
        event_id
        for event_id in turn_record.source_event_ids
        if (existing_record := existing_records.get(event_id)) is not None
        and existing_record.completed
        and not same_turn_identity(existing_record, turn_record)
    )
    if conflicting_source_event_ids:
        projected_turn_record = _project_redaction_alias(turn_record, conflicting_source_event_ids)
        if projected_turn_record is None:
            return None
        turn_record = projected_turn_record
    same_identity_records = (
        existing_record
        for event_id in turn_record.indexed_event_ids
        if (existing_record := existing_records.get(event_id)) is not None
        and same_turn_identity(existing_record, turn_record)
    )
    highest_precedence_existing_record = max(
        same_identity_records,
        key=lambda record: (record.completed, record.timestamp),
        default=None,
    )
    resolved_record = (
        _merge_same_identity_records(turn_record, highest_precedence_existing_record)
        if highest_precedence_existing_record is not None
        else turn_record
    )
    discovery_event_ids = tuple(
        event_id
        for event_id in resolved_record.discovery_event_ids
        if (existing_record := existing_records.get(event_id)) is None
        or not existing_record.completed
        or same_turn_identity(existing_record, resolved_record)
    )
    return canonicalize_turn_record(resolved_record, discovery_event_ids=discovery_event_ids)


def _project_redaction_alias(
    turn_record: TurnRecord,
    conflicting_source_event_ids: tuple[str, ...],
) -> TurnRecord | None:
    """Detach redaction markers from source aliases now owned by another completed turn."""
    if not turn_record.redacted_source_event_ids:
        return None
    conflicting_ids = set(conflicting_source_event_ids)
    retained_source_event_ids = tuple(
        event_id for event_id in turn_record.source_event_ids if event_id not in conflicting_ids
    )
    if not retained_source_event_ids:
        retained_source_event_ids = tuple(
            event_id for event_id in turn_record.redacted_source_event_ids if event_id not in conflicting_ids
        )
    if not retained_source_event_ids:
        return None
    anchor_event_id = (
        turn_record.anchor_event_id
        if turn_record.anchor_event_id in retained_source_event_ids
        else retained_source_event_ids[-1]
    )
    return canonicalize_turn_record(
        turn_record,
        source_event_ids=retained_source_event_ids,
        anchor_event_id=anchor_event_id,
        source_event_metadata=(
            {}
            if turn_record.is_coalesced and turn_record.source_event_metadata is None
            else turn_record.source_event_metadata
        ),
        # Turn-level requester context remains required for owed redaction cleanup; an explicit
        # empty source map keeps per-source replay ownership fail-closed after projection.
        requester_id=turn_record.requester_id,
    )


def _merge_same_identity_records(candidate: TurnRecord, existing: TurnRecord) -> TurnRecord:
    """Keep the newer same-turn record while preserving older echo and discovery facts."""
    if candidate.completed != existing.completed:
        newer, older = (candidate, existing) if candidate.completed else (existing, candidate)
    else:
        newer, older = (candidate, existing) if candidate.timestamp > existing.timestamp else (existing, candidate)
    return canonicalize_turn_record(
        newer,
        discovery_event_ids=(*newer.discovery_event_ids, *older.discovery_event_ids),
        redacted_source_event_ids=(
            *newer.redacted_source_event_ids,
            *older.redacted_source_event_ids,
        ),
        visible_echo_event_id=newer.visible_echo_event_id or older.visible_echo_event_id,
        visible_echo_is_fallback=(
            newer.visible_echo_is_fallback
            if newer.visible_echo_is_fallback is not None
            else older.visible_echo_is_fallback
        ),
        command_execution_started=newer.command_execution_started or older.command_execution_started,
        command_result_text=newer.command_result_text or older.command_result_text,
        latest_edit_receipt_order=max(
            newer.latest_edit_receipt_order or 0,
            older.latest_edit_receipt_order or 0,
        )
        or None,
        user_stop_receipt_order=max(
            newer.user_stop_receipt_order or 0,
            older.user_stop_receipt_order or 0,
        )
        or None,
        user_stop_settled_receipt_order=max(
            newer.user_stop_settled_receipt_order or 0,
            older.user_stop_settled_receipt_order or 0,
        )
        or None,
    )


def _bool_or_none(value: object) -> bool | None:
    """Return a strict boolean or None."""
    return value if isinstance(value, bool) else None


def _positive_int_or_none(value: object) -> int | None:
    """Return one positive non-boolean integer or None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _mapping_or_none(value: object) -> Mapping[str, Any] | None:
    """Return a typed mapping for codec input."""
    return typing.cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else None


def _responses_file_path(base_path: Path, agent_name: str) -> Path:
    """Return the lexically validated ledger path for one agent."""
    if not agent_name or ".." in agent_name or "/" in agent_name or "\\" in agent_name:
        message = f"Invalid handled-turn ledger agent name: {agent_name!r}"
        raise ValueError(message)
    return base_path / f"{agent_name}_responded.json"


@dataclass(frozen=True)
class _ResponseGroup:
    """Logical handled-turn group keyed by its complete indexed identity."""

    timestamp: float
    records: dict[str, TurnRecord]


def _response_group_requires_retention(
    group: _ResponseGroup,
    unsettled_source_event_ids: frozenset[str],
) -> bool:
    """Return whether one group still owns unfinished durable work."""
    return (
        not unsettled_source_event_ids.isdisjoint(group.records)
        or any(record.pending_redaction_cleanup_event_ids for record in group.records.values())
        or any(not record.completed and record.replay_source_event_ids for record in group.records.values())
        or any(
            record.user_stop_receipt_order is not None
            and (record.user_stop_settled_receipt_order or 0) < record.user_stop_receipt_order
            for record in group.records.values()
        )
    )


def _cleaned_responses(
    responses: dict[str, TurnRecord],
    *,
    max_events: int,
    max_age_days: int,
    unsettled_source_event_ids: Collection[str] = (),
) -> dict[str, TurnRecord]:
    """Remove stale turn groups while keeping coalesced groups intact."""
    current_time = time.time()
    max_age_seconds = max_age_days * 24 * 60 * 60
    retained_source_event_ids = frozenset(unsettled_source_event_ids)
    fresh_groups = [
        group
        for group in _response_groups(responses)
        if _response_group_requires_retention(group, retained_source_event_ids)
        or current_time - group.timestamp < max_age_seconds
    ]
    if len(fresh_groups) > max_events:
        retained_groups = [
            group for group in fresh_groups if _response_group_requires_retention(group, retained_source_event_ids)
        ]
        retained_group_ids = {id(group) for group in retained_groups}
        ordinary_groups = [group for group in fresh_groups if id(group) not in retained_group_ids]
        kept_ordinary_groups = ordinary_groups[-max_events:] if max_events else []
        fresh_groups = sorted((*retained_groups, *kept_ordinary_groups), key=lambda group: group.timestamp)
    cleaned_responses: dict[str, TurnRecord] = {}
    for group in fresh_groups:
        cleaned_responses.update(group.records)
    return cleaned_responses


def _response_groups(responses: dict[str, TurnRecord]) -> list[_ResponseGroup]:
    """Return handled turns grouped by canonical sources and discovery aliases."""
    grouped_records: dict[tuple[str, ...], dict[str, TurnRecord]] = {}
    grouped_timestamps: dict[tuple[str, ...], float] = {}
    for event_id, record in responses.items():
        grouped_records.setdefault(record.indexed_event_ids, {})[event_id] = record
        grouped_timestamps[record.indexed_event_ids] = max(
            grouped_timestamps.get(record.indexed_event_ids, 0.0),
            record.timestamp,
        )
    return sorted(
        (
            _ResponseGroup(
                timestamp=grouped_timestamps[indexed_event_ids],
                records=records,
            )
            for indexed_event_ids, records in grouped_records.items()
        ),
        key=lambda group: group.timestamp,
    )
