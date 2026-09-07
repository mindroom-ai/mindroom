"""Atomic persistence for pending Matrix join/decrypt fences."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_RECORD_VERSION = "mindroom-sync-continuity-v4"
_LEGACY_RECORD_VERSIONS = ("mindroom-sync-continuity-v2", "mindroom-sync-continuity-v3")


@dataclass(frozen=True)
class SyncContinuityRecord:
    """One crash-atomic pending join/decrypt fence snapshot."""

    revision: int = 0
    pending_join_decrypt_fences: frozenset[str] = frozenset()


class SyncContinuityStore:
    """Own serialized fresh-read updates to one agent's pending join fences."""

    def __init__(self, storage_path: Path, agent_name: str) -> None:
        self._path = storage_path / "sync_continuity" / f"{agent_name}.json"
        self._lock_path = self._path.with_suffix(f"{self._path.suffix}.lock")

    def load(self) -> SyncContinuityRecord:
        """Load pending join fences under the shared cross-process lock."""
        with advisory_file_lock(self._lock_path, exclusive=True):
            return self._load_locked()

    def update_join_fences(
        self,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
        retain: Iterable[str] | None = None,
    ) -> SyncContinuityRecord:
        """Transform join fences from fresh durable state."""
        added = frozenset(_normalize_room_id(room_id) for room_id in add)
        removed = frozenset(_normalize_room_id(room_id) for room_id in remove)
        retained = None if retain is None else frozenset(_normalize_room_id(room_id) for room_id in retain)

        with advisory_file_lock(self._lock_path, exclusive=True):
            current = self._load_locked()
            fences = current.pending_join_decrypt_fences
            if retained is not None:
                fences &= retained
            fences = (fences | added) - removed
            if fences == current.pending_join_decrypt_fences:
                return current
            updated = SyncContinuityRecord(
                revision=current.revision + 1,
                pending_join_decrypt_fences=fences,
            )
            self._save_locked(updated)
            return updated

    def _save_locked(self, record: SyncContinuityRecord) -> None:
        write_json_file_durable(
            self._path,
            {
                "pending_join_decrypt_fences": sorted(record.pending_join_decrypt_fences),
                "revision": record.revision,
                "version": _RECORD_VERSION,
            },
            strict_atomic_replace=True,
            sort_keys=True,
            trailing_newline=True,
        )

    def _load_locked(self) -> SyncContinuityRecord:
        """Load one record while its advisory lock is held."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SyncContinuityRecord()
        except UnicodeDecodeError as exc:
            raise _format_error(self._path, "invalid UTF-8") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _format_error(self._path, "invalid JSON") from exc
        if not isinstance(payload, dict):
            raise _format_error(self._path, "unsupported version")
        version = payload.get("version")
        legacy = version in _LEGACY_RECORD_VERSIONS
        expected_fields = {"pending_join_decrypt_fences", "revision", "version"}
        if legacy:
            expected_fields.add("checkpoint")
        elif version != _RECORD_VERSION:
            raise _format_error(self._path, "unsupported version")

        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise _format_error(self._path, "invalid revision")

        raw_fences = payload.get("pending_join_decrypt_fences")
        if (
            not isinstance(raw_fences, list)
            or any(not isinstance(room_id, str) or not room_id for room_id in raw_fences)
            or len(set(cast("list[str]", raw_fences))) != len(raw_fences)
            or set(payload) != expected_fields
        ):
            raise _format_error(self._path, "invalid join fences")
        record = SyncContinuityRecord(
            revision=revision + int(legacy),
            pending_join_decrypt_fences=frozenset(cast("list[str]", raw_fences)),
        )
        if legacy:
            # Only the join fences remain app-owned. Nio establishes its own
            # baseline; importing this checkpoint would skip unadmitted input.
            self._save_locked(record)
        return record


def _normalize_room_id(room_id: str) -> str:
    if not room_id:
        msg = "Pending join decrypt fences require a non-empty room ID"
        raise ValueError(msg)
    return room_id


def _format_error(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Invalid Matrix sync continuity record at {path}: {detail}")
