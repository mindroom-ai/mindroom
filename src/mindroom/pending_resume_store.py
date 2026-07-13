"""Durable ledger of in-flight visible turns for restart auto-resume.

Every visible response attempt records its placeholder here the moment the
placeholder exists, and discards the record when the turn settles in a
user-visible terminal state. Records that survive a process death are the
turns that were in flight at shutdown, so startup can replay auto-resume
prompts within seconds instead of waiting for the full stale-stream scan.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.message_target import MessageTarget

logger = get_logger(__name__)

_LEDGER_FILENAME = "pending_resumes.json"


@dataclass(frozen=True)
class PendingResumeRecord:
    """One persisted in-flight visible turn that may need restart auto-resume."""

    agent_name: str
    room_id: str
    thread_id: str
    target_event_id: str
    requester_user_id: str | None
    created_at_ms: int

    @property
    def key(self) -> str:
        """Return the one-record-per-conversation ledger key."""
        return f"{self.agent_name}|{self.room_id}|{self.thread_id}"

    def to_payload(self) -> dict[str, object]:
        """Return the JSON-safe ledger payload for this record."""
        return {
            "agent_name": self.agent_name,
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "target_event_id": self.target_event_id,
            "requester_user_id": self.requester_user_id,
            "created_at_ms": self.created_at_ms,
        }

    @classmethod
    def from_payload(cls, payload: object) -> PendingResumeRecord | None:
        """Return one validated record from a raw ledger payload."""
        if not isinstance(payload, dict):
            return None
        fields = cast("dict[str, object]", payload)
        agent_name = fields.get("agent_name")
        room_id = fields.get("room_id")
        thread_id = fields.get("thread_id")
        target_event_id = fields.get("target_event_id")
        requester_user_id = fields.get("requester_user_id")
        created_at_ms = fields.get("created_at_ms")
        if (
            not isinstance(agent_name, str)
            or not agent_name
            or not isinstance(room_id, str)
            or not room_id
            or not isinstance(thread_id, str)
            or not thread_id
            or not isinstance(target_event_id, str)
            or not target_event_id
            or not isinstance(created_at_ms, int)
        ):
            return None
        if requester_user_id is not None and not isinstance(requester_user_id, str):
            return None
        return cls(
            agent_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            target_event_id=target_event_id,
            requester_user_id=requester_user_id or None,
            created_at_ms=created_at_ms,
        )


def pending_resume_ledger_path(runtime_paths: RuntimePaths) -> Path:
    """Return the pending auto-resume ledger path for one runtime context."""
    return tracking_dir(runtime_paths) / _LEDGER_FILENAME


def load_pending_resume_records(ledger_path: Path) -> dict[str, PendingResumeRecord]:
    """Load all valid ledger records keyed by conversation, tolerating a missing or corrupt file."""
    try:
        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Ignoring unreadable pending-resume ledger", ledger_path=str(ledger_path), error=str(exc))
        return {}
    if not isinstance(raw, dict):
        logger.warning("Ignoring malformed pending-resume ledger", ledger_path=str(ledger_path))
        return {}

    records: dict[str, PendingResumeRecord] = {}
    for key, payload in raw.items():
        record = PendingResumeRecord.from_payload(payload)
        if isinstance(key, str) and record is not None and record.key == key:
            records[key] = record
    return records


def _upsert_pending_resume_record(ledger_path: Path, record: PendingResumeRecord) -> None:
    """Store one record, replacing any prior record for the same conversation."""
    records = load_pending_resume_records(ledger_path)
    records[record.key] = record
    _write_records(ledger_path, records)


def discard_pending_resume_records(ledger_path: Path, keys: Iterable[str]) -> None:
    """Remove the given conversation keys from the ledger when present."""
    records = load_pending_resume_records(ledger_path)
    remaining = {key: record for key, record in records.items() if key not in set(keys)}
    if len(remaining) != len(records):
        _write_records(ledger_path, remaining)


def _write_records(ledger_path: Path, records: dict[str, PendingResumeRecord]) -> None:
    """Durably replace the ledger contents."""
    write_json_file_durable(
        ledger_path,
        {key: record.to_payload() for key, record in sorted(records.items())},
        indent=2,
    )


@dataclass(frozen=True)
class PendingResumeTracker:
    """Record one agent's in-flight visible turns for restart auto-resume.

    Ledger I/O failures are contained here: losing a resume intent must never
    break the response turn that tried to record it.
    """

    ledger_path: Path
    agent_name: str

    def note_started(
        self,
        target_event_id: str,
        *,
        target: MessageTarget,
        requester_user_id: str | None,
    ) -> None:
        """Persist one in-flight visible turn; room-level turns are not resumable."""
        if target.resolved_thread_id is None:
            return
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

    def note_settled(self, target: MessageTarget, *, resumable: bool) -> None:
        """Drop the turn's record when it settled in a user-visible terminal state."""
        if resumable or target.resolved_thread_id is None:
            return
        key = f"{self.agent_name}|{target.room_id}|{target.resolved_thread_id}"
        try:
            discard_pending_resume_records(self.ledger_path, (key,))
        except Exception as exc:
            logger.warning("Failed to discard pending-resume record", key=key, error=str(exc))
