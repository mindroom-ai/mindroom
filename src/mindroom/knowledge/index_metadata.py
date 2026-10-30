"""The two persisted state documents of one knowledge base, each with one writer.

``publication.json`` names what search serves: the collection, the settings its vectors were built under, and the
corpus signature and revision it matches. Only the publisher writes it, while it holds the base's source-root lock:
the refresh child, or the runtime's files-mode publication after a dashboard upload or delete.

``refresh_status.json`` records what the runtime knows about refreshing that publication: a source change not yet
published, the last refresh failure, and when a refresh last finished. Only runtime processes write it, never the
refresh child, each write a load-modify-save under the base's status lock. Whether a refresh is running is not stored:
the runtime's scheduler knows.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeIs

from mindroom.file_locks import advisory_file_lock
from mindroom.knowledge.indexing_config import IndexingSettings, published_index_settings_compatible

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

_PUBLICATION_FILE = "publication.json"
_REFRESH_STATUS_FILE = "refresh_status.json"
_REFRESH_STATUS_LOCK_FILE = "refresh_status.lock"


@dataclass(frozen=True)
class Publication:
    """What one knowledge base's search serves."""

    settings: IndexingSettings
    #: The collection holding the vectors; None for a files-mode base, which publishes its source listing only.
    collection: str | None
    indexed_count: int
    source_signature: str
    published_revision: str | None
    last_published_at: str
    #: The collection this publication replaced, kept for the next refresh to take over; None when it kept none,
    #: as after a forced rebuild, which trusts no older collection.
    retired: str | None = None

    def serves(self, expected: IndexingSettings) -> bool:
        """Return whether this publication's vectors can answer queries made under ``expected``."""
        return self.collection is not None and published_index_settings_compatible(self.settings, expected)


@dataclass(frozen=True)
class RefreshStatus:
    """What the runtime knows about refreshing a base's publication."""

    #: Why the source is known to have changed since the last publication, or None.
    reason: str | None = None
    #: Incremented by every source-change mark, so a refresh clears only the marks made before it started.
    reason_generation: int = 0
    last_error: str | None = None
    consecutive_refresh_failures: int = 0
    last_refresh_at: str | None = None


def utc_now() -> str:
    """Return the current time as stored in knowledge state."""
    return datetime.now(tz=UTC).isoformat()


def publication_path(storage_path: Path) -> Path:
    """Return the publication file of the base stored at ``storage_path``."""
    return storage_path / _PUBLICATION_FILE


def new_publication(
    *,
    settings: IndexingSettings,
    collection: str | None,
    indexed_count: int,
    source_signature: str,
    published_revision: str | None,
    retired: str | None = None,
) -> Publication:
    """Return the publication a refresh makes now; both publish paths build it here."""
    return Publication(
        settings=settings,
        collection=collection,
        indexed_count=indexed_count,
        source_signature=source_signature,
        published_revision=published_revision,
        last_published_at=utc_now(),
        retired=retired,
    )


def load_publication(storage_path: Path) -> Publication | None:
    """Return the base's publication, or None when it has none or its file does not parse.

    Callers that must tell the two apart check whether ``publication_path`` exists: an unreadable publication proves
    nothing about which collection is live.
    """
    payload = _load_object(publication_path(storage_path))
    if payload is None:
        return None
    settings = _settings(payload.get("settings"))
    collection = payload.get("collection")
    indexed_count = payload.get("indexed_count")
    source_signature = payload.get("source_signature")
    published_revision = payload.get("published_revision")
    last_published_at = payload.get("last_published_at")
    retired = payload.get("retired")
    if (
        settings is None
        or not (collection is None or (isinstance(collection, str) and collection))
        or (collection is None and settings.mode != "files")
        or not _count(indexed_count)
        or not isinstance(source_signature, str)
        or not source_signature
        or not (published_revision is None or (isinstance(published_revision, str) and published_revision))
        or not isinstance(last_published_at, str)
        or not (retired is None or (isinstance(retired, str) and retired))
    ):
        return None
    return Publication(
        settings=settings,
        collection=collection,
        indexed_count=indexed_count,
        source_signature=source_signature,
        published_revision=published_revision,
        last_published_at=last_published_at,
        retired=retired,
    )


def save_publication(storage_path: Path, publication: Publication) -> None:
    """Atomically replace the base's publication."""
    write_json_atomic(
        publication_path(storage_path),
        {
            "settings": publication.settings.to_metadata(),
            "collection": publication.collection,
            "indexed_count": publication.indexed_count,
            "source_signature": publication.source_signature,
            "published_revision": publication.published_revision,
            "last_published_at": publication.last_published_at,
            "retired": publication.retired,
        },
    )


def load_refresh_status(storage_path: Path) -> RefreshStatus:
    """Return the base's refresh status; a missing or unreadable file reads as nothing known."""
    payload = _load_object(storage_path / _REFRESH_STATUS_FILE)
    if payload is None:
        return RefreshStatus()
    reason = payload.get("reason")
    last_error = payload.get("last_error")
    last_refresh_at = payload.get("last_refresh_at")
    reason_generation = payload.get("reason_generation")
    failures = payload.get("consecutive_refresh_failures")
    return RefreshStatus(
        reason=reason if isinstance(reason, str) and reason else None,
        reason_generation=reason_generation if _count(reason_generation) else 0,
        last_error=last_error if isinstance(last_error, str) and last_error else None,
        consecutive_refresh_failures=failures if _count(failures) else 0,
        last_refresh_at=last_refresh_at if isinstance(last_refresh_at, str) and last_refresh_at else None,
    )


def update_refresh_status(storage_path: Path, change: Callable[[RefreshStatus], RefreshStatus]) -> RefreshStatus:
    """Apply ``change`` to the base's refresh status under its lock, which orders threads and processes alike."""
    storage_path.mkdir(parents=True, exist_ok=True)
    with advisory_file_lock(storage_path / _REFRESH_STATUS_LOCK_FILE):
        status = change(load_refresh_status(storage_path))
        write_json_atomic(
            storage_path / _REFRESH_STATUS_FILE,
            {
                "reason": status.reason,
                "reason_generation": status.reason_generation,
                "last_error": status.last_error,
                "consecutive_refresh_failures": status.consecutive_refresh_failures,
                "last_refresh_at": status.last_refresh_at,
            },
        )
    return status


def marked_stale(status: RefreshStatus, reason: str) -> RefreshStatus:
    """Return ``status`` recording a source change, which supersedes the last failure until a refresh reports again."""
    return replace(status, reason=reason, reason_generation=status.reason_generation + 1, last_error=None)


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """Write one JSON document so readers only ever see a complete file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_object(path: Path) -> dict[str, object] | None:
    # ``ValueError`` rather than ``JSONDecodeError``: invalid UTF-8 raises ``UnicodeDecodeError``, a ``ValueError``.
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _settings(raw_settings: object) -> IndexingSettings | None:
    if not isinstance(raw_settings, dict):
        return None
    metadata: dict[str, str] = {}
    for key, value in raw_settings.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        metadata[key] = value
    return IndexingSettings.from_metadata(metadata)


def _count(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
