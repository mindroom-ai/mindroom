"""Atomic persistence for Matrix checkpoints and pending join fences."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger
from mindroom.matrix.sync_token_values import SyncCheckpoint, normalize_sync_token

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

logger = get_logger(__name__)

_RECORD_VERSION = "mindroom-sync-continuity-v3"

# Versions this file used to be written in. A record in one of them is an
# upgrade rather than damage, and it is refused for the same reason either way:
# the checkpoint it holds certifies a store this build does not have, so
# resuming from it would claim the journal contains events it never admitted.
#
# Refusing is right and the consequence is not free, so it is said out loud
# rather than left to a line that reads like a corrupt file. The next sync is
# tokenless, nio classifies every event an initial sync returns as history, and
# admission takes history as context rather than as work -- so a message sent
# while this install was stopped is present in the conversation and is never
# answered. One boot only: the record is rewritten in the current version
# immediately, and every later restart resumes normally.
_PREVIOUS_RECORD_VERSIONS = frozenset({"mindroom-sync-continuity-v2"})


@dataclass(frozen=True)
class SyncContinuityRecord:
    """One crash-atomic Matrix checkpoint and join-fence snapshot."""

    revision: int = 0
    checkpoint: SyncCheckpoint | None = None
    pending_join_decrypt_fences: frozenset[str] = frozenset()


class SyncContinuityStore:
    """Own serialized fresh-read updates to one agent's continuity record."""

    def __init__(self, storage_path: Path, agent_name: str) -> None:
        self._path = storage_path / "sync_continuity" / f"{agent_name}.json"
        self._lock_path = self._path.with_suffix(f"{self._path.suffix}.lock")

    def load(self) -> SyncContinuityRecord:
        """Load current continuity under the shared cross-process lock."""
        with advisory_file_lock(self._lock_path, exclusive=False):
            return self._load_locked()

    def replace_checkpoint(self, checkpoint: SyncCheckpoint) -> SyncContinuityRecord:
        """Replace only the checkpoint from fresh durable state."""
        normalized = _normalize_checkpoint(checkpoint)
        return self._update(lambda current: replace(current, checkpoint=normalized))

    def clear_checkpoint(self) -> SyncContinuityRecord:
        """Clear checkpoint trust and repair an invalid record to cold state."""
        return self._update(
            lambda current: replace(current, checkpoint=None),
            recover_invalid=True,
        )

    def update_join_fences(
        self,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
        retain: Iterable[str] | None = None,
    ) -> SyncContinuityRecord:
        """Transform only join fences from fresh durable state."""
        added = frozenset(_normalize_room_id(room_id) for room_id in add)
        removed = frozenset(_normalize_room_id(room_id) for room_id in remove)
        retained = None if retain is None else frozenset(_normalize_room_id(room_id) for room_id in retain)

        def transform(current: SyncContinuityRecord) -> SyncContinuityRecord:
            fences = current.pending_join_decrypt_fences
            if retained is not None:
                fences &= retained
            return replace(current, pending_join_decrypt_fences=(fences | added) - removed)

        return self._update(transform)

    def accept_classic_response(
        self,
        checkpoint: SyncCheckpoint,
        *,
        joined_room_ids: Iterable[str],
    ) -> SyncContinuityRecord:
        """Atomically advance Classic continuity and settle observed join fences."""
        normalized_checkpoint = _normalize_checkpoint(checkpoint)
        joined = frozenset(_normalize_room_id(room_id) for room_id in joined_room_ids)
        return self._update(
            lambda current: SyncContinuityRecord(
                revision=current.revision,
                checkpoint=normalized_checkpoint,
                pending_join_decrypt_fences=current.pending_join_decrypt_fences - joined,
            ),
        )

    def _update(
        self,
        transform: Callable[[SyncContinuityRecord], SyncContinuityRecord],
        *,
        recover_invalid: bool = False,
    ) -> SyncContinuityRecord:
        """Serialize one transform over freshly loaded durable state."""
        with advisory_file_lock(self._lock_path, exclusive=True):
            invalid_record = False
            try:
                current = self._load_locked()
            except RuntimeError:
                if not recover_invalid:
                    raise
                current = SyncContinuityRecord()
                invalid_record = True
            transformed = replace(transform(current), revision=current.revision)
            if transformed == current and not invalid_record:
                return current
            updated = replace(transformed, revision=current.revision + 1)
            write_json_file_durable(
                self._path,
                _record_payload(updated),
                strict_atomic_replace=True,
                sort_keys=True,
                trailing_newline=True,
            )
            return updated

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
        if not isinstance(payload, dict) or payload.get("version") != _RECORD_VERSION:
            _warn_when_written_by_a_previous_version(self._path, payload)
            raise _format_error(self._path, "unsupported version")

        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise _format_error(self._path, "invalid revision")

        raw_checkpoint = payload.get("checkpoint")
        if raw_checkpoint is None:
            checkpoint = None
        elif isinstance(raw_checkpoint, dict):
            token = normalize_sync_token(raw_checkpoint.get("token"))
            store_generation = normalize_sync_token(raw_checkpoint.get("store_generation"))
            if token is None or store_generation is None or set(raw_checkpoint) != {"store_generation", "token"}:
                raise _format_error(self._path, "invalid checkpoint")
            checkpoint = SyncCheckpoint(token=token, store_generation=store_generation)
        else:
            raise _format_error(self._path, "invalid checkpoint")

        raw_fences = payload.get("pending_join_decrypt_fences")
        if (
            not isinstance(raw_fences, list)
            or any(not isinstance(room_id, str) or not room_id for room_id in raw_fences)
            or len(set(cast("list[str]", raw_fences))) != len(raw_fences)
            or set(payload)
            != {
                "checkpoint",
                "pending_join_decrypt_fences",
                "revision",
                "version",
            }
        ):
            raise _format_error(self._path, "invalid join fences")
        return SyncContinuityRecord(
            revision=revision,
            checkpoint=checkpoint,
            pending_join_decrypt_fences=frozenset(cast("list[str]", raw_fences)),
        )


def _record_payload(record: SyncContinuityRecord) -> dict[str, object]:
    checkpoint = record.checkpoint
    checkpoint_payload: dict[str, str] | None
    if checkpoint is None:
        checkpoint_payload = None
    else:
        checkpoint_payload = {
            "store_generation": cast("str", checkpoint.store_generation),
            "token": checkpoint.token,
        }
    return {
        "checkpoint": checkpoint_payload,
        "pending_join_decrypt_fences": sorted(record.pending_join_decrypt_fences),
        "revision": record.revision,
        "version": _RECORD_VERSION,
    }


def _normalize_checkpoint(checkpoint: SyncCheckpoint) -> SyncCheckpoint:
    token = normalize_sync_token(checkpoint.token)
    store_generation = normalize_sync_token(checkpoint.store_generation)
    if token is None or store_generation is None:
        msg = "Sync continuity checkpoints require a token and store generation"
        raise ValueError(msg)
    return SyncCheckpoint(token=token, store_generation=store_generation)


def _normalize_room_id(room_id: str) -> str:
    if not room_id:
        msg = "Pending join decrypt fences require a non-empty room ID"
        raise ValueError(msg)
    return room_id


def _warn_when_written_by_a_previous_version(path: Path, payload: object) -> None:
    """Say what refusing an earlier version's record costs, when that is what happened."""
    if not isinstance(payload, dict):
        return
    # JSON object keys are strings by construction; the narrowed type is not.
    found_version = cast("dict[str, object]", payload).get("version")
    if found_version not in _PREVIOUS_RECORD_VERSIONS:
        return
    logger.warning(
        "matrix_sync_continuity_record_upgraded",
        path=str(path),
        found_version=found_version,
        record_version=_RECORD_VERSION,
        consequence=(
            "This install's Matrix sync position was written by an earlier version and cannot be "
            "resumed from. The next sync starts without a token, so any message sent while this "
            "install was stopped stays in the conversation but is never answered. This affects "
            "this startup only."
        ),
    )


def _format_error(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Invalid Matrix sync continuity record at {path}: {detail}")
