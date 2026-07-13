"""Durable in-flight response records used by restart auto-resume."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, cast

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.message_target import MessageTarget

logger = get_logger(__name__)


@dataclass(frozen=True)
class PendingResumeRecord:
    """One visible threaded response that had not settled when persisted."""

    agent_name: str
    room_id: str
    thread_id: str
    target_event_id: str
    requester_user_id: str | None
    created_at_ms: int

    @property
    def key(self) -> str:
        """Return the one-record-per-conversation key."""
        return f"{self.agent_name}|{self.room_id}|{self.thread_id}"

    @classmethod
    def from_payload(cls, payload: object) -> PendingResumeRecord | None:
        """Parse a record written by this module."""
        if not isinstance(payload, dict):
            return None
        try:
            record = cls(**cast("dict[str, Any]", payload))
        except TypeError:
            return None
        if (
            all(
                isinstance(value, str) and value
                for value in (record.agent_name, record.room_id, record.thread_id, record.target_event_id)
            )
            and (record.requester_user_id is None or isinstance(record.requester_user_id, str))
            and isinstance(record.created_at_ms, int)
            and not isinstance(record.created_at_ms, bool)
        ):
            return record
        return None


def pending_resume_ledger_path(runtime_paths: RuntimePaths) -> Path:
    """Return the shared pending-resume ledger path."""
    return tracking_dir(runtime_paths) / "pending_resumes.json"


def load_pending_resume_records(path: Path) -> dict[str, PendingResumeRecord]:
    """Load valid records, tolerating a missing or corrupt ledger."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Ignoring unreadable pending-resume ledger", path=str(path), error=str(exc))
        return {}
    if not isinstance(payload, dict):
        return {}
    parsed = ((key, PendingResumeRecord.from_payload(value)) for key, value in payload.items())
    return {key: record for key, record in parsed if isinstance(key, str) and record is not None and key == record.key}


def _upsert_pending_resume_record(path: Path, record: PendingResumeRecord) -> None:
    """Replace the pending response for one conversation."""
    records = load_pending_resume_records(path)
    records[record.key] = record
    _write_records(path, records)


def discard_pending_resume_records(path: Path, expected: Iterable[PendingResumeRecord]) -> None:
    """Delete only exact record versions, preserving concurrent replacements."""
    expected_by_key = {record.key: record for record in expected}
    records = load_pending_resume_records(path)
    remaining = {key: record for key, record in records.items() if expected_by_key.get(key) != record}
    if remaining != records:
        _write_records(path, remaining)


def _write_records(path: Path, records: dict[str, PendingResumeRecord]) -> None:
    write_json_file_durable(path, {key: asdict(record) for key, record in sorted(records.items())}, indent=2)


@dataclass(frozen=True)
class PendingResumeTracker:
    """Best-effort lifecycle writes for one response-running entity."""

    ledger_path: Path
    agent_name: str

    def note_started(
        self,
        target_event_id: str,
        *,
        target: MessageTarget,
        requester_user_id: str | None,
    ) -> PendingResumeRecord | None:
        """Persist a visible threaded response and return its exact version."""
        if target.resolved_thread_id is None:
            return None
        record = PendingResumeRecord(
            agent_name=self.agent_name,
            room_id=target.room_id,
            thread_id=target.resolved_thread_id,
            target_event_id=target_event_id,
            requester_user_id=requester_user_id,
            created_at_ms=int(time.time() * 1000),
        )
        try:
            _upsert_pending_resume_record(self.ledger_path, record)
        except Exception as exc:
            logger.warning("Failed to persist pending-resume record", key=record.key, error=str(exc))
        return record

    def note_settled(self, record: PendingResumeRecord | None, *, resumable: bool) -> None:
        """Discard a record unless restart recovery may still need it."""
        if resumable or record is None:
            return
        try:
            discard_pending_resume_records(self.ledger_path, (record,))
        except Exception as exc:
            logger.warning("Failed to discard pending-resume record", key=record.key, error=str(exc))
