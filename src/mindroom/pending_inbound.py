"""Track restart-safe inbound claims for one agent."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import typing
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, TypedDict

from mindroom.logging_config import get_logger

logger = get_logger(__name__)


class _SerializedPendingInboundRecord(TypedDict):
    """JSON-safe durable state for one inbound source event."""

    timestamp: float
    room_id: str
    event_source: dict[str, Any]


@dataclass(frozen=True)
class PendingInboundReplay:
    """One inbound source event that is still safe to replay after restart."""

    event_id: str
    room_id: str
    event_source: dict[str, Any]


@dataclass
class PendingInboundStore:
    """Persist and query non-terminal inbound claims for one runtime entity."""

    agent_name: str
    base_path: Path
    _records: dict[str, _SerializedPendingInboundRecord] = field(default_factory=dict, init=False)
    _records_file: Path = field(init=False)
    _records_lock_file: Path = field(init=False)
    _thread_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize paths and load any existing inbound claims."""
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._records_file = _records_file_path(self.base_path, self.agent_name)
        self._records_lock_file = self._records_file.with_suffix(f"{self._records_file.suffix}.lock")
        self._load_records()
        self._cleanup_old_records()

    def claim(self, *, room_id: str, event_source: dict[str, Any]) -> bool:
        """Record one replayable inbound event, returning False when it was already claimed."""
        event_id = _normalized_event_id(event_source.get("event_id"))
        if event_id is None:
            msg = "Cannot claim a pending inbound event without an event_id"
            raise ValueError(msg)
        record = _serialized_record(room_id=room_id, event_source=event_source)
        with self._thread_lock, self._file_lock(exclusive=True):
            self._records = self._read_records_locked()
            if event_id in self._records:
                return False
            self._records[event_id] = record
            self._save_records_locked()
        logger.debug(
            "pending_inbound_claimed",
            agent=self.agent_name,
            event_id=event_id,
            room_id=record["room_id"],
        )
        return True

    def remove(self, event_ids: typing.Sequence[str]) -> None:
        """Delete any tracked inbound claims for the provided source events."""
        normalized_event_ids = _normalize_event_ids(event_ids)
        if not normalized_event_ids:
            return
        changed = False
        with self._thread_lock, self._file_lock(exclusive=True):
            self._records = self._read_records_locked()
            for event_id in normalized_event_ids:
                if self._records.pop(event_id, None) is not None:
                    changed = True
            if changed:
                self._save_records_locked()
        if changed:
            logger.debug(
                "pending_inbound_removed",
                agent=self.agent_name,
                event_ids=list(normalized_event_ids),
            )

    def pending_replays(self) -> list[PendingInboundReplay]:
        """Return inbound events that are still safe to replay."""
        with self._thread_lock, self._file_lock(exclusive=False):
            self._records = self._read_records_locked(repair_corrupt_file=False)
            replays = [
                (
                    record["timestamp"],
                    PendingInboundReplay(
                        event_id=event_id,
                        room_id=record["room_id"],
                        event_source=dict(record["event_source"]),
                    ),
                )
                for event_id, record in self._records.items()
            ]
        return [replay for _timestamp, replay in sorted(replays, key=lambda item: (item[0], item[1].event_id))]

    def contains(self, event_id: str) -> bool:
        """Return whether one inbound event is still tracked as replayable."""
        normalized_event_id = _normalized_event_id(event_id)
        if normalized_event_id is None:
            return False
        with self._thread_lock, self._file_lock(exclusive=False):
            self._records = self._read_records_locked(repair_corrupt_file=False)
            return normalized_event_id in self._records

    def _load_records(self) -> None:
        """Load pending inbound state from disk."""
        with self._thread_lock, self._file_lock(exclusive=True):
            self._records = self._read_records_locked()

    def _save_records_locked(self) -> None:
        """Persist pending inbound state while the thread and file locks are held."""
        temp_path = None
        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.base_path,
                prefix=f"{self._records_file.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = self.base_path / Path(temp_file.name).name
                json.dump(self._records, temp_file, indent=2)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            temp_path.replace(self._records_file)
            self._fsync_base_path()
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()

    def _cleanup_old_records(self, max_events: int = 10000, max_age_days: int = 30) -> None:
        """Drop stale replayable claims so the restart inbox stays bounded."""
        current_time = time.time()
        max_age_seconds = max_age_days * 24 * 60 * 60
        with self._thread_lock, self._file_lock(exclusive=True):
            self._records = self._read_records_locked()
            fresh_items = [
                (event_id, record)
                for event_id, record in self._records.items()
                if current_time - record["timestamp"] < max_age_seconds
            ]
            if len(fresh_items) > max_events:
                fresh_items = sorted(fresh_items, key=lambda item: item[1]["timestamp"])[-max_events:]
            self._records = dict(fresh_items)
            self._save_records_locked()
        logger.info(
            "pending_inbound_cleanup_completed",
            agent=self.agent_name,
            kept_event_count=len(self._records),
        )

    @contextmanager
    def _file_lock(self, *, exclusive: bool) -> typing.Iterator[None]:
        """Lock the store for cross-instance readers and writers."""
        with self._records_lock_file.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_records_locked(
        self,
        *,
        repair_corrupt_file: bool = True,
    ) -> dict[str, _SerializedPendingInboundRecord]:
        """Read and normalize persisted records while the file lock is held."""
        if not self._records_file.exists():
            return {}
        try:
            with self._records_file.open(encoding="utf-8") as records_file:
                data = json.load(records_file)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._invalid_records_result(
                repair_corrupt_file=repair_corrupt_file,
                repaired_message="Quarantined malformed pending inbound file",
                shared_read_message="Detected malformed pending inbound file during shared read",
            )
        if not isinstance(data, dict):
            return self._invalid_records_result(
                repair_corrupt_file=repair_corrupt_file,
                repaired_message="Quarantined structurally invalid pending inbound file",
                shared_read_message="Detected structurally invalid pending inbound file during shared read",
                payload_type=type(data).__name__,
            )
        normalized_records, invalid_event_ids = _normalize_records_payload(data)
        if invalid_event_ids:
            self._log_invalid_event_entries(
                invalid_event_ids=invalid_event_ids,
                repair_corrupt_file=repair_corrupt_file,
            )
        return normalized_records

    def _invalid_records_result(
        self,
        *,
        repair_corrupt_file: bool,
        repaired_message: str,
        shared_read_message: str,
        payload_type: str | None = None,
    ) -> dict[str, _SerializedPendingInboundRecord]:
        """Log one invalid persisted state condition and return an empty snapshot."""
        log_kwargs = {
            "agent": self.agent_name,
            "records_file": str(self._records_file),
        }
        if payload_type is not None:
            log_kwargs["payload_type"] = payload_type
        if repair_corrupt_file:
            quarantined_file = self._quarantine_corrupt_records_file_locked()
            logger.warning(
                repaired_message,
                quarantined_file=str(quarantined_file or self._records_file),
                **log_kwargs,
            )
            return {}
        logger.warning(shared_read_message, **log_kwargs)
        return {}

    def _log_invalid_event_entries(
        self,
        *,
        invalid_event_ids: list[str],
        repair_corrupt_file: bool,
    ) -> None:
        """Log one invalid-entry condition after normalizing the persisted payload."""
        log_kwargs = {
            "agent": self.agent_name,
            "records_file": str(self._records_file),
            "invalid_event_ids": invalid_event_ids,
        }
        if repair_corrupt_file:
            quarantined_file = self._quarantine_corrupt_records_file_locked()
            logger.warning(
                "Quarantined pending inbound file with invalid event entries",
                quarantined_file=str(quarantined_file or self._records_file),
                **log_kwargs,
            )
            return
        logger.warning(
            "Detected pending inbound file with invalid event entries during shared read",
            **log_kwargs,
        )

    def _quarantine_corrupt_records_file_locked(self) -> Path | None:
        """Move a corrupt pending inbound file aside while the file lock is held."""
        quarantined_file = self.base_path / f"{self._records_file.name}.corrupt-{time.time_ns()}"
        try:
            self._records_file.replace(quarantined_file)
        except FileNotFoundError:
            return None
        return quarantined_file

    def _fsync_base_path(self) -> None:
        """Flush the tracking directory so atomic replacements are durable."""
        base_dir_fd = os.open(self.base_path, os.O_RDONLY)
        try:
            os.fsync(base_dir_fd)
        finally:
            os.close(base_dir_fd)


def _normalize_event_ids(event_ids: typing.Sequence[str]) -> tuple[str, ...]:
    """Deduplicate event IDs while preserving order."""
    normalized_event_ids: list[str] = []
    seen_event_ids: set[str] = set()
    for event_id in event_ids:
        if not isinstance(event_id, str) or not event_id or event_id in seen_event_ids:
            continue
        seen_event_ids.add(event_id)
        normalized_event_ids.append(event_id)
    return tuple(normalized_event_ids)


def _normalized_event_id(event_id: object) -> str | None:
    """Return a non-empty Matrix event ID or None."""
    return event_id if isinstance(event_id, str) and event_id else None


def _normalized_room_id(room_id: object) -> str | None:
    """Return a non-empty Matrix room ID or None."""
    return room_id if isinstance(room_id, str) and room_id else None


def _serialized_record(
    *,
    room_id: str,
    event_source: dict[str, Any],
) -> _SerializedPendingInboundRecord:
    """Return one normalized serialized pending inbound record."""
    normalized_room_id = _normalized_room_id(room_id)
    if normalized_room_id is None:
        msg = "Pending inbound records require a room_id"
        raise ValueError(msg)
    if not isinstance(event_source, dict):
        msg = "Pending inbound records require a dict event source"
        raise TypeError(msg)
    return {
        "timestamp": time.time(),
        "room_id": normalized_room_id,
        "event_source": dict(event_source),
    }


def _normalize_serialized_record(
    raw_record: dict[str, Any],
) -> _SerializedPendingInboundRecord | None:
    """Normalize one on-disk pending inbound record into the current schema."""
    room_id = _normalized_room_id(raw_record.get("room_id"))
    event_source = raw_record.get("event_source")
    timestamp = raw_record.get("timestamp")
    legacy_state = raw_record.get("state")
    if room_id is None or not isinstance(event_source, dict):
        return None
    if legacy_state is not None and legacy_state not in {"pending", "started"}:
        return None
    return {
        "timestamp": float(timestamp) if isinstance(timestamp, int | float) else 0.0,
        "room_id": room_id,
        "event_source": dict(event_source),
    }


def _normalize_records_payload(
    data: dict[object, object],
) -> tuple[dict[str, _SerializedPendingInboundRecord], list[str]]:
    """Normalize the full on-disk payload into valid records plus invalid keys."""
    normalized_records: dict[str, _SerializedPendingInboundRecord] = {}
    invalid_event_ids: list[str] = []
    for event_id, record in data.items():
        if not isinstance(event_id, str) or not isinstance(record, dict):
            invalid_event_ids.append(event_id if isinstance(event_id, str) else repr(event_id))
            continue
        normalized_record = _normalize_serialized_record(typing.cast("dict[str, Any]", record))
        if normalized_record is None:
            invalid_event_ids.append(event_id)
            continue
        normalized_records[event_id] = normalized_record
    return normalized_records, invalid_event_ids


def _records_file_path(base_path: Path, agent_name: str) -> Path:
    """Return the validated store path for one agent."""
    if not agent_name or ".." in agent_name or "/" in agent_name or "\\" in agent_name:
        message = f"Invalid pending inbound agent name: {agent_name!r}"
        raise ValueError(message)
    records_file = base_path / f"{agent_name}_pending_inbound.json"
    if records_file.resolve().parent != base_path.resolve():
        message = f"Invalid pending inbound path for agent: {agent_name!r}"
        raise ValueError(message)
    return records_file
