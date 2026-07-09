"""Persist canonical turn records for one runtime entity.

Reads are served from in-memory state shared across every ledger bound to the
same responses file, so sibling ledger instances in one process observe each
other's writes without touching the filesystem. Disk persistence happens on a
single write-behind worker thread that merges exact records into the file under
an advisory lock, keeping cross-process writers safe while the event loop never
blocks on filesystem I/O (issue #1260).
"""

from __future__ import annotations

import json
import threading
import time
import typing
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from mindroom import constants
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.history.types import HistoryScope
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.timestamp_formatting import normalize_timestamp_ms

if typing.TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = get_logger(__name__)

_TURN_RECORD_SCHEMA_VERSION = 1
_LEDGER_SCHEMA_VERSION_KEY = "schema_version"
_LEDGER_RECORDS_KEY = "records"


@dataclass(frozen=True)
class SourceEventMetadata:
    """Durable model-facing metadata for one source Matrix event."""

    sender: str
    timestamp_ms: float | None = None

    def __post_init__(self) -> None:
        """Normalize the timestamp once for every physical representation."""
        object.__setattr__(self, "timestamp_ms", normalize_timestamp_ms(self.timestamp_ms))

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
        return cls(sender=sender, timestamp_ms=normalize_timestamp_ms(metadata.get("timestamp_ms")))


@dataclass(frozen=True)
class TurnRecord:
    """Canonical immutable identity, outcome, and regeneration facts for one turn."""

    source_event_ids: tuple[str, ...]
    anchor_event_id: str | None = None
    response_event_id: str | None = None
    completed: bool = True
    visible_echo_event_id: str | None = None
    source_event_prompts: Mapping[str, str] | None = None
    source_event_metadata: Mapping[str, SourceEventMetadata] | None = None
    response_owner: str | None = None
    requester_id: str | None = None
    correlation_id: str | None = None
    history_scope: HistoryScope | None = None
    conversation_target: MessageTarget | None = None
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        """Normalize every construction path into the canonical schema once."""
        source_event_ids = _normalize_source_event_ids(self.source_event_ids)
        anchor_event_id = _normalize_string(self.anchor_event_id)
        if anchor_event_id is None and source_event_ids:
            anchor_event_id = source_event_ids[-1]
        timestamp = self.timestamp
        normalized_timestamp = (
            float(timestamp) if isinstance(timestamp, int | float) and not isinstance(timestamp, bool) else 0.0
        )
        object.__setattr__(self, "source_event_ids", source_event_ids)
        object.__setattr__(self, "anchor_event_id", anchor_event_id)
        object.__setattr__(self, "response_event_id", _normalize_string(self.response_event_id))
        object.__setattr__(self, "visible_echo_event_id", _normalize_string(self.visible_echo_event_id))
        object.__setattr__(
            self,
            "source_event_prompts",
            _immutable_prompt_map(source_event_ids, self.source_event_prompts),
        )
        object.__setattr__(
            self,
            "source_event_metadata",
            _immutable_source_event_metadata(source_event_ids, self.source_event_metadata),
        )
        object.__setattr__(self, "response_owner", _normalize_string(self.response_owner))
        object.__setattr__(self, "requester_id", _normalize_string(self.requester_id))
        object.__setattr__(self, "correlation_id", _normalize_string(self.correlation_id))
        object.__setattr__(
            self,
            "history_scope",
            self.history_scope if isinstance(self.history_scope, HistoryScope) else None,
        )
        object.__setattr__(
            self,
            "conversation_target",
            self.conversation_target if isinstance(self.conversation_target, MessageTarget) else None,
        )
        object.__setattr__(self, "timestamp", normalized_timestamp)

    @classmethod
    def create(
        cls,
        source_event_ids: Sequence[str],
        *,
        anchor_event_id: str | None = None,
        response_event_id: str | None = None,
        completed: bool = True,
        visible_echo_event_id: str | None = None,
        source_event_prompts: Mapping[str, str] | None = None,
        source_event_metadata: Mapping[str, object] | None = None,
        response_owner: str | None = None,
        requester_id: str | None = None,
        correlation_id: str | None = None,
        history_scope: HistoryScope | None = None,
        conversation_target: MessageTarget | None = None,
        timestamp: float = 0.0,
    ) -> TurnRecord:
        """Create a record while accepting sequence and mapping inputs from runtime flows."""
        return cls(
            source_event_ids=tuple(source_event_ids),
            anchor_event_id=anchor_event_id,
            response_event_id=response_event_id,
            completed=completed,
            visible_echo_event_id=visible_echo_event_id,
            source_event_prompts=source_event_prompts,
            source_event_metadata=typing.cast("Mapping[str, SourceEventMetadata] | None", source_event_metadata),
            response_owner=response_owner,
            requester_id=requester_id,
            correlation_id=correlation_id,
            history_scope=history_scope,
            conversation_target=conversation_target,
            timestamp=timestamp,
        )

    @property
    def is_coalesced(self) -> bool:
        """Return whether the turn combines multiple source events."""
        return len(self.source_event_ids) > 1


class TurnRecordCodec:
    """Encode the canonical record into its two intentional physical projections."""

    @staticmethod
    def schema_version() -> int:
        """Return the persisted schema version emitted by this codec."""
        return _TURN_RECORD_SCHEMA_VERSION

    @staticmethod
    def to_ledger_record(record: TurnRecord) -> dict[str, object]:
        """Serialize one exact record for the versioned handled-turn ledger."""
        payload: dict[str, object] = {
            "anchor_event_id": record.anchor_event_id,
            "source_event_ids": list(record.source_event_ids),
            "response_event_id": record.response_event_id,
            "completed": record.completed,
            "timestamp": record.timestamp,
        }
        if record.visible_echo_event_id is not None:
            payload["visible_echo_event_id"] = record.visible_echo_event_id
        if record.source_event_prompts is not None:
            payload["source_event_prompts"] = dict(record.source_event_prompts)
        if record.source_event_metadata is not None:
            payload["source_event_metadata"] = {
                event_id: metadata.to_record() for event_id, metadata in record.source_event_metadata.items()
            }
        if record.response_owner is not None:
            payload["response_owner"] = record.response_owner
        if record.requester_id is not None:
            payload["requester_id"] = record.requester_id
        if record.correlation_id is not None:
            payload["correlation_id"] = record.correlation_id
        if record.history_scope is not None:
            payload["history_scope"] = record.history_scope.to_metadata()
        if record.conversation_target is not None:
            payload["conversation_target"] = record.conversation_target.to_metadata()
        return payload

    @staticmethod
    def from_ledger_record(event_id: str, raw_record: object) -> TurnRecord | None:
        """Parse one record from the current ledger schema without legacy migration."""
        if not isinstance(raw_record, Mapping):
            return None
        record = typing.cast("Mapping[str, object]", raw_record)
        raw_source_event_ids = record.get("source_event_ids")
        anchor_event_id = record.get("anchor_event_id")
        completed = record.get("completed")
        timestamp = record.get("timestamp")
        response_event_id = record.get("response_event_id")
        if (
            not isinstance(raw_source_event_ids, list)
            or not isinstance(anchor_event_id, str)
            or not anchor_event_id
            or not isinstance(completed, bool)
            or not isinstance(timestamp, int | float)
            or isinstance(timestamp, bool)
            or (response_event_id is not None and not isinstance(response_event_id, str))
        ):
            return None
        source_event_ids = _normalize_source_event_ids(raw_source_event_ids)
        turn_record = TurnRecord.create(
            source_event_ids,
            anchor_event_id=anchor_event_id,
            response_event_id=response_event_id,
            completed=completed,
            visible_echo_event_id=_normalize_string(record.get("visible_echo_event_id")),
            source_event_prompts=_mapping_or_none(record.get("source_event_prompts")),
            source_event_metadata=_mapping_or_none(record.get("source_event_metadata")),
            response_owner=_normalize_string(record.get("response_owner")),
            requester_id=_normalize_string(record.get("requester_id")),
            correlation_id=_normalize_string(record.get("correlation_id")),
            history_scope=HistoryScope.from_metadata(record.get("history_scope")),
            conversation_target=MessageTarget.from_metadata(record.get("conversation_target")),
            timestamp=float(timestamp),
        )
        if event_id not in turn_record.source_event_ids:
            return None
        return turn_record

    @staticmethod
    def to_run_metadata(record: TurnRecord) -> dict[str, object]:
        """Project one record into the recoverable subset stored with an Agno run."""
        if not record.source_event_ids:
            return {}
        metadata: dict[str, object] = {
            constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY: TurnRecordCodec.schema_version(),
            constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: list(record.source_event_ids),
        }
        if record.source_event_prompts is not None:
            metadata[constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY] = dict(record.source_event_prompts)
        if record.source_event_metadata is not None:
            metadata[constants.MATRIX_SOURCE_EVENT_METADATA_KEY] = {
                event_id: source_metadata.to_record()
                for event_id, source_metadata in record.source_event_metadata.items()
            }
        if record.response_owner is not None:
            metadata[constants.MATRIX_RESPONSE_OWNER_METADATA_KEY] = record.response_owner
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
        source_event_ids = (
            _normalize_source_event_ids(raw_source_event_ids)
            if isinstance(raw_source_event_ids, list)
            else (anchor_event_id,)
        ) or (anchor_event_id,)
        response_event_id = _normalize_string(metadata.get(constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY))
        return TurnRecord.create(
            source_event_ids,
            anchor_event_id=anchor_event_id,
            response_event_id=response_event_id,
            completed=response_event_id is not None,
            source_event_prompts=_mapping_or_none(metadata.get(constants.MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY)),
            source_event_metadata=_mapping_or_none(metadata.get(constants.MATRIX_SOURCE_EVENT_METADATA_KEY)),
            response_owner=_normalize_string(metadata.get(constants.MATRIX_RESPONSE_OWNER_METADATA_KEY)),
            requester_id=_normalize_string(metadata.get("requester_id")),
            correlation_id=_normalize_string(metadata.get("correlation_id")),
            history_scope=HistoryScope.from_metadata(metadata.get(constants.MATRIX_HISTORY_SCOPE_METADATA_KEY)),
            conversation_target=MessageTarget.from_metadata(
                metadata.get(constants.MATRIX_CONVERSATION_TARGET_METADATA_KEY),
            ),
        )


@dataclass
class _LedgerState:
    """In-memory canonical records shared by every ledger bound to one file."""

    responses: dict[str, TurnRecord] = field(default_factory=dict)
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
    """Store exact canonical records without owning merge or recovery policy."""

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

    def warm(self) -> None:
        """Load and compact the persisted ledger; call from a worker thread, not the event loop."""
        self._cleanup_old_events()

    def flush(self) -> None:
        """Block until every scheduled best-effort persist attempt has completed."""
        with self._state.lock:
            self._wait_for_pending_persists_locked()

    def record_handled_turn(self, turn_record: TurnRecord) -> None:
        """Persist one exact record for every source event in the turn."""
        if not turn_record.source_event_ids:
            return
        persisted_record = turn_record if turn_record.timestamp != 0.0 else replace(turn_record, timestamp=time.time())
        with self._state.lock:
            self._ensure_loaded_locked()
            for event_id in persisted_record.source_event_ids:
                self._responses[event_id] = persisted_record
            self._schedule_persist_locked(persisted_record.source_event_ids)
        logger.debug("handled_turn_recorded", source_event_count=len(persisted_record.source_event_ids))

    def has_responded(self, event_id: str) -> bool:
        """Return whether the source event has a terminal recorded outcome."""
        with self._state.lock:
            self._ensure_loaded_locked()
            record = self._responses.get(event_id)
            return record.completed if record is not None else False

    def get_visible_echo_event_id(self, source_event_id: str) -> str | None:
        """Return the tracked visible echo event ID for one source event."""
        with self._state.lock:
            self._ensure_loaded_locked()
            record = self._responses.get(source_event_id)
            return record.visible_echo_event_id if record is not None else None

    def visible_echo_event_id_for_sources(self, source_event_ids: Sequence[str]) -> str | None:
        """Return the first visible echo already tracked for one or more source events."""
        with self._state.lock:
            self._ensure_loaded_locked()
            for event_id in _normalize_source_event_ids(source_event_ids):
                record = self._responses.get(event_id)
                if record is not None and record.visible_echo_event_id is not None:
                    return record.visible_echo_event_id
        return None

    def get_turn_record(self, source_event_id: str) -> TurnRecord | None:
        """Return the canonical record for one source event."""
        with self._state.lock:
            self._ensure_loaded_locked()
            return self._responses.get(source_event_id)

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

    def _persist_records(self, records: dict[str, TurnRecord]) -> None:
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

    def _write_responses_file_locked(self, responses: dict[str, TurnRecord]) -> None:
        """Atomically write one versioned ledger payload while the file lock is held."""
        payload = {
            _LEDGER_SCHEMA_VERSION_KEY: TurnRecordCodec.schema_version(),
            _LEDGER_RECORDS_KEY: {
                event_id: TurnRecordCodec.to_ledger_record(record) for event_id, record in responses.items()
            },
        }
        write_json_file_durable(self._responses_file, payload, temp_dir=self.base_path, indent=2)

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
            record = TurnRecordCodec.from_ledger_record(event_id, raw_record) if isinstance(event_id, str) else None
            if record is None:
                invalid_event_ids.append(event_id if isinstance(event_id, str) else repr(event_id))
                continue
            records[event_id] = record
        rehydrated_records = {
            source_event_id: record for record in records.values() for source_event_id in record.source_event_ids
        }
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


def _normalize_source_event_ids(source_event_ids: Sequence[object]) -> tuple[str, ...]:
    """Deduplicate non-empty source event IDs while preserving order."""
    normalized_event_ids: list[str] = []
    seen_event_ids: set[str] = set()
    for event_id in source_event_ids:
        if not isinstance(event_id, str) or not event_id or event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        normalized_event_ids.append(event_id)
    return tuple(normalized_event_ids)


def _normalize_string(value: object) -> str | None:
    """Return a non-empty string or None."""
    return value if isinstance(value, str) and value else None


def _mapping_or_none(value: object) -> Mapping[str, Any] | None:
    """Return a typed mapping for codec input."""
    return typing.cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else None


def _immutable_prompt_map(
    source_event_ids: tuple[str, ...],
    source_event_prompts: Mapping[str, str] | None,
) -> Mapping[str, str] | None:
    """Freeze prompt entries that belong to the canonical source identity."""
    if not source_event_prompts:
        return None
    prompt_map = {
        event_id: prompt
        for event_id in source_event_ids
        if isinstance((prompt := source_event_prompts.get(event_id)), str)
    }
    return MappingProxyType(prompt_map) if prompt_map else None


def _immutable_source_event_metadata(
    source_event_ids: tuple[str, ...],
    source_event_metadata: Mapping[str, SourceEventMetadata] | None,
) -> Mapping[str, SourceEventMetadata] | None:
    """Normalize and freeze source metadata belonging to the canonical identity."""
    if not source_event_metadata:
        return None
    metadata: dict[str, SourceEventMetadata] = {}
    for event_id in source_event_ids:
        raw_metadata = source_event_metadata.get(event_id)
        normalized = (
            raw_metadata
            if isinstance(raw_metadata, SourceEventMetadata)
            else SourceEventMetadata.from_raw(raw_metadata)
        )
        if normalized is not None:
            metadata[event_id] = normalized
    return MappingProxyType(metadata) if metadata else None


def _responses_file_path(base_path: Path, agent_name: str) -> Path:
    """Return the lexically validated ledger path for one agent."""
    if not agent_name or ".." in agent_name or "/" in agent_name or "\\" in agent_name:
        message = f"Invalid handled-turn ledger agent name: {agent_name!r}"
        raise ValueError(message)
    return base_path / f"{agent_name}_responded.json"


@dataclass(frozen=True)
class _ResponseGroup:
    """Logical handled-turn group keyed by its canonical source identity."""

    timestamp: float
    records: dict[str, TurnRecord]


def _cleaned_responses(
    responses: dict[str, TurnRecord],
    *,
    max_events: int,
    max_age_days: int,
) -> dict[str, TurnRecord]:
    """Remove stale turn groups while keeping coalesced groups intact."""
    current_time = time.time()
    max_age_seconds = max_age_days * 24 * 60 * 60
    fresh_groups = [group for group in _response_groups(responses) if current_time - group.timestamp < max_age_seconds]
    if len(fresh_groups) > max_events:
        fresh_groups = fresh_groups[-max_events:]
    cleaned_responses: dict[str, TurnRecord] = {}
    for group in fresh_groups:
        cleaned_responses.update(group.records)
    return cleaned_responses


def _response_groups(responses: dict[str, TurnRecord]) -> list[_ResponseGroup]:
    """Return handled turns grouped by canonical source-event identity."""
    grouped_records: dict[tuple[str, ...], dict[str, TurnRecord]] = {}
    grouped_timestamps: dict[tuple[str, ...], float] = {}
    for event_id, record in responses.items():
        grouped_records.setdefault(record.source_event_ids, {})[event_id] = record
        grouped_timestamps[record.source_event_ids] = max(
            grouped_timestamps.get(record.source_event_ids, 0.0),
            record.timestamp,
        )
    return sorted(
        (
            _ResponseGroup(
                timestamp=grouped_timestamps[source_event_ids],
                records=records,
            )
            for source_event_ids, records in grouped_records.items()
        ),
        key=lambda group: group.timestamp,
    )
