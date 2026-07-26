"""Durable candidate-index checkpoint for resumable semantic knowledge refreshes.

A semantic refresh builds its vectors into a private *candidate* collection and
only publishes when the candidate provably matches the live source. Without
durable candidate state every interruption (restart, cancellation, one failed
embedding request) throws away all completed work, so a corpus that takes
longer to index than the interval between interruptions never publishes.

This module owns the on-disk representation of that candidate:

* ``candidate_index.json`` is a compacted snapshot written atomically.
* ``candidate_index.jsonl`` is an append-only journal of per-file updates
  applied since the snapshot, so recording one completed file costs one small
  append instead of rewriting a snapshot that grows with the corpus.

Loading replays the journal over the snapshot; a torn trailing line from a
crash is ignored. Compaction rewrites the snapshot and then removes the
journal, and replaying an already-compacted journal is idempotent.

The checkpoint never makes a candidate queryable: publication still goes
through the published-index metadata, and a candidate is only ever resumed when
its recorded identity and settings fingerprint match the current runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from mindroom.knowledge.index_metadata import write_json_atomic
from mindroom.knowledge.indexing_config import IndexingSettings

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

#: Bumped whenever the persisted candidate layout changes incompatibly. An
#: unknown version is treated as "no resumable candidate" rather than an error.
_CANDIDATE_CHECKPOINT_SCHEMA_VERSION = 1

_CANDIDATE_CHECKPOINT_FILENAME = "candidate_index.json"
_CANDIDATE_JOURNAL_FILENAME = "candidate_index.jsonl"

#: ``(source_mtime_ns, source_size, source_content_digest)`` for one source file.
#: The content digest is what makes resume safe across Git checkouts, which
#: rewrite mtimes without changing content.
FileSignature = tuple[int, int, str]

_CandidateStatus = Literal["building", "failed"]


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(frozen=True, slots=True)
class CandidateFailure:
    """Retry bookkeeping for one source file that could not be indexed."""

    attempts: int = 0
    last_error: str | None = None
    last_attempt_at: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateCheckpoint:
    """Resumable state for one in-progress candidate index."""

    collection: str
    settings: IndexingSettings
    status: _CandidateStatus = "building"
    target_revision: str | None = None
    total_files: int = 0
    completed: Mapping[str, FileSignature] = field(default_factory=dict)
    failed: Mapping[str, CandidateFailure] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    #: Journal entries replayed when this checkpoint was loaded. Not persisted:
    #: it exists so a resumed run inherits the compaction bound instead of
    #: restarting the count and letting the journal grow without limit across
    #: repeated hard kills.
    replayed_journal_entries: int = 0

    @property
    def completed_count(self) -> int:
        """Return how many source files the candidate has recorded as indexed."""
        return len(self.completed)


def _candidate_checkpoint_path(base_storage_path: Path) -> Path:
    """Return the compacted candidate snapshot path for one knowledge base."""
    return base_storage_path / _CANDIDATE_CHECKPOINT_FILENAME


def _candidate_journal_path(base_storage_path: Path) -> Path:
    """Return the append-only candidate journal path for one knowledge base."""
    return base_storage_path / _CANDIDATE_JOURNAL_FILENAME


def _parse_signature(value: object) -> FileSignature | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    source_mtime_ns, source_size, source_digest = value
    if isinstance(source_mtime_ns, bool) or isinstance(source_size, bool):
        return None
    if not isinstance(source_mtime_ns, int) or not isinstance(source_size, int) or source_size < 0:
        return None
    if not isinstance(source_digest, str) or not source_digest:
        return None
    return source_mtime_ns, source_size, source_digest


def _parse_failure(value: object) -> CandidateFailure | None:
    if not isinstance(value, dict):
        return None
    failure = cast("dict[str, object]", value)
    attempts = failure.get("attempts")
    last_error = failure.get("last_error")
    last_attempt_at = failure.get("last_attempt_at")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        return None
    return CandidateFailure(
        attempts=attempts,
        last_error=last_error if isinstance(last_error, str) and last_error else None,
        last_attempt_at=last_attempt_at if isinstance(last_attempt_at, str) and last_attempt_at else None,
    )


def _snapshot_payload(checkpoint: CandidateCheckpoint) -> dict[str, object]:
    return {
        "schema_version": _CANDIDATE_CHECKPOINT_SCHEMA_VERSION,
        "collection": checkpoint.collection,
        "status": checkpoint.status,
        "settings": checkpoint.settings.to_metadata(),
        "target_revision": checkpoint.target_revision,
        "total_files": checkpoint.total_files,
        "completed": {
            relative_path: list(signature) for relative_path, signature in sorted(checkpoint.completed.items())
        },
        "failed": {
            relative_path: {
                "attempts": failure.attempts,
                "last_error": failure.last_error,
                "last_attempt_at": failure.last_attempt_at,
            }
            for relative_path, failure in sorted(checkpoint.failed.items())
        },
        "created_at": checkpoint.created_at,
        "updated_at": checkpoint.updated_at,
    }


def _parse_settings(raw_settings: object) -> IndexingSettings | None:
    if not isinstance(raw_settings, dict):
        return None
    settings_metadata: dict[str, str] = {}
    for key, value in raw_settings.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        settings_metadata[key] = value
    return IndexingSettings.from_metadata(settings_metadata)


def _parse_completed(raw_completed: object) -> dict[str, FileSignature]:
    completed: dict[str, FileSignature] = {}
    if not isinstance(raw_completed, dict):
        return completed
    for relative_path, raw_signature in raw_completed.items():
        signature = _parse_signature(raw_signature)
        if isinstance(relative_path, str) and signature is not None:
            completed[relative_path] = signature
    return completed


def _parse_failed(raw_failed: object) -> dict[str, CandidateFailure]:
    failed: dict[str, CandidateFailure] = {}
    if not isinstance(raw_failed, dict):
        return failed
    for relative_path, raw_failure in raw_failed.items():
        failure = _parse_failure(raw_failure)
        if isinstance(relative_path, str) and failure is not None:
            failed[relative_path] = failure
    return failed


def _parse_snapshot(payload: Mapping[str, object]) -> CandidateCheckpoint | None:
    if payload.get("schema_version") != _CANDIDATE_CHECKPOINT_SCHEMA_VERSION:
        return None
    collection = payload.get("collection")
    status = payload.get("status")
    if not isinstance(collection, str) or not collection or status not in {"building", "failed"}:
        return None
    settings = _parse_settings(payload.get("settings"))
    if settings is None:
        return None

    completed = _parse_completed(payload.get("completed"))
    failed = _parse_failed(payload.get("failed"))
    target_revision = payload.get("target_revision")
    total_files = payload.get("total_files")
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    return CandidateCheckpoint(
        collection=collection,
        settings=settings,
        status="failed" if status == "failed" else "building",
        target_revision=target_revision if isinstance(target_revision, str) and target_revision else None,
        total_files=total_files if isinstance(total_files, int) and not isinstance(total_files, bool) else 0,
        completed=completed,
        failed=failed,
        created_at=created_at if isinstance(created_at, str) else "",
        updated_at=updated_at if isinstance(updated_at, str) else "",
    )


def _journal_entries(journal_path: Path) -> list[Mapping[str, object]]:
    try:
        raw_text = journal_path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[Mapping[str, object]] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            # A crash can tear the final append; earlier entries stay valid.
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _apply_journal_entry(
    entry: Mapping[str, object],
    completed: dict[str, FileSignature],
    failed: dict[str, CandidateFailure],
) -> None:
    relative_path = entry.get("path")
    if not isinstance(relative_path, str) or not relative_path:
        return
    if entry.get("removed") is True:
        completed.pop(relative_path, None)
        failed.pop(relative_path, None)
        return
    signature = _parse_signature(entry.get("signature"))
    if signature is not None:
        completed[relative_path] = signature
        failed.pop(relative_path, None)
        return
    failure = _parse_failure(entry.get("failure"))
    if failure is not None:
        completed.pop(relative_path, None)
        failed[relative_path] = failure


def load_candidate_checkpoint(base_storage_path: Path) -> CandidateCheckpoint | None:
    """Load the persisted candidate, replaying journal updates over the snapshot."""
    snapshot_path = _candidate_checkpoint_path(base_storage_path)
    try:
        raw_payload = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw_payload, dict):
        return None
    checkpoint = _parse_snapshot(raw_payload)
    if checkpoint is None:
        return None

    completed = dict(checkpoint.completed)
    failed = dict(checkpoint.failed)
    entries = _journal_entries(_candidate_journal_path(base_storage_path))
    for entry in entries:
        _apply_journal_entry(entry, completed, failed)
    return replace(
        checkpoint,
        completed=completed,
        failed=failed,
        replayed_journal_entries=len(entries),
    )


def save_candidate_checkpoint(base_storage_path: Path, checkpoint: CandidateCheckpoint) -> CandidateCheckpoint:
    """Atomically write a compacted snapshot and drop the superseded journal."""
    now = _utc_now()
    compacted = replace(
        checkpoint,
        created_at=checkpoint.created_at or now,
        updated_at=now,
        replayed_journal_entries=0,
    )
    base_storage_path.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_candidate_checkpoint_path(base_storage_path), _snapshot_payload(compacted))
    # Ordering matters: the snapshot lands first, so a crash here only replays
    # entries the snapshot already contains.
    _candidate_journal_path(base_storage_path).unlink(missing_ok=True)
    return compacted


def append_candidate_journal(
    base_storage_path: Path,
    *,
    completed: Iterable[tuple[str, FileSignature]] = (),
    failed: Iterable[tuple[str, CandidateFailure]] = (),
    removed: Iterable[str] = (),
) -> None:
    """Append per-file candidate updates without rewriting the snapshot."""
    lines: list[str] = []
    for relative_path, signature in completed:
        lines.append(json.dumps({"path": relative_path, "signature": list(signature)}, sort_keys=True))
    for relative_path, failure in failed:
        lines.append(
            json.dumps(
                {
                    "path": relative_path,
                    "failure": {
                        "attempts": failure.attempts,
                        "last_error": failure.last_error,
                        "last_attempt_at": failure.last_attempt_at,
                    },
                },
                sort_keys=True,
            ),
        )
    lines.extend(json.dumps({"path": relative_path, "removed": True}, sort_keys=True) for relative_path in removed)
    if not lines:
        return
    base_storage_path.mkdir(parents=True, exist_ok=True)
    with _candidate_journal_path(base_storage_path).open("a", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in lines))


def delete_candidate_checkpoint(base_storage_path: Path) -> None:
    """Remove candidate state once the candidate is published or discarded."""
    _candidate_checkpoint_path(base_storage_path).unlink(missing_ok=True)
    _candidate_journal_path(base_storage_path).unlink(missing_ok=True)
