"""The one persisted knowledge index state document.

Each knowledge base keeps exactly one state file,
``<storage_root>/knowledge_db/<storage_key>/indexing_settings.json``, and this
module owns its whole schema: one dataclass, one loader, one writer.

Two collaborators write that file. The indexing manager records a publication
(which collection is live, what it contains, which revision produced it), and
the registry records the refresh job around it (queued, running, failed, and
the error and timestamps that go with it). Both go through
``save_published_index_state`` with a *whole* state, so neither can reset the
other's fields merely by not knowing about them.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from mindroom.knowledge.indexing_config import IndexingSettings

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


_PublishedIndexStatus = Literal["resetting", "indexing", "complete", "failed"]
_RefreshJob = Literal["idle", "pending", "running", "failed"]

_PUBLISHED_INDEX_STATUSES: frozenset[str] = frozenset({"resetting", "indexing", "complete", "failed"})
_REFRESH_JOBS: frozenset[str] = frozenset({"idle", "pending", "running", "failed"})


@dataclass(frozen=True)
class PublishedIndexState:
    """Persisted state for the published knowledge index.

    ``settings`` through ``source_signature`` describe one publication;
    ``refresh_job`` through ``consecutive_refresh_failures`` describe the
    refresh job that produced or is producing it.
    """

    settings: IndexingSettings
    status: _PublishedIndexStatus
    collection: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    indexed_count: int | None = None
    source_signature: str | None = None
    refresh_job: _RefreshJob = "idle"
    reason: str | None = None
    last_error: str | None = None
    updated_at: str | None = None
    last_refresh_at: str | None = None
    consecutive_refresh_failures: int = 0


def load_published_index_state(metadata_path: Path) -> PublishedIndexState | None:
    """Load the persisted state, or ``None`` when the file holds no usable state.

    A field the payload does not carry becomes ``None`` rather than rejecting
    the document, so an in-progress or failed record keeps everything it does
    say -- including the collection a previous publication left live, which is
    the only on-disk proof of which collection candidate cleanup must spare.

    Two things can still refuse a payload. Settings and status give every other
    field its meaning, so a record missing either identifies nothing. And a
    record that calls itself ``complete`` without proving what it published is
    corrupt rather than merely thin: see ``_records_a_publication``.
    """
    payload = _load_payload(metadata_path)
    if payload is None:
        return None
    settings = _parse_settings(payload.get("settings"))
    status = payload.get("status")
    if settings is None or status not in _PUBLISHED_INDEX_STATUSES:
        return None
    state = PublishedIndexState(
        settings=settings,
        status=cast("_PublishedIndexStatus", status),
        collection=_optional_str(payload.get("collection")),
        last_published_at=_optional_str(payload.get("last_published_at")),
        published_revision=_optional_str(payload.get("published_revision")),
        indexed_count=_nonnegative_int(payload.get("indexed_count")),
        source_signature=_optional_str(payload.get("source_signature")),
        refresh_job=_refresh_job(payload.get("refresh_job")),
        reason=_optional_str(payload.get("reason")),
        last_error=_optional_str(payload.get("last_error")),
        updated_at=_optional_str(payload.get("updated_at")),
        last_refresh_at=_optional_str(payload.get("last_refresh_at")),
        consecutive_refresh_failures=_nonnegative_int(payload.get("consecutive_refresh_failures")) or 0,
    )
    if state.status == "complete" and not _records_a_publication(state):
        return None
    return state


def _records_a_publication(state: PublishedIndexState) -> bool:
    """Return whether a ``complete`` record proves the publication it claims.

    Every writer of a ``complete`` record states how many files it indexed and
    the corpus signature it indexed them from; a semantic base also names the
    collection holding the vectors, while a file-mode base publishes source
    metadata and has none. A record missing any of those describes an index
    nothing can check, so it is treated as corrupt and the base is refreshed
    rather than served from a publication it cannot substantiate.
    """
    return (
        state.indexed_count is not None
        and state.source_signature is not None
        and (state.collection is not None or state.settings.mode == "files")
    )


def save_published_index_state(metadata_path: Path, state: PublishedIndexState) -> None:
    """Atomically persist one whole state, so no field is lost by omission."""
    optional_fields: dict[str, object | None] = {
        "collection": state.collection,
        "last_published_at": state.last_published_at,
        "published_revision": state.published_revision,
        "indexed_count": state.indexed_count,
        "source_signature": state.source_signature,
        "reason": state.reason,
        "last_error": state.last_error,
        "updated_at": state.updated_at,
        "last_refresh_at": state.last_refresh_at,
    }
    write_json_atomic(
        metadata_path,
        {
            "settings": state.settings.to_metadata(),
            "status": state.status,
            "refresh_job": state.refresh_job,
            "consecutive_refresh_failures": state.consecutive_refresh_failures,
            **{name: value for name, value in optional_fields.items() if value is not None},
        },
    )


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


def _load_payload(metadata_path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _parse_settings(raw_settings: object) -> IndexingSettings | None:
    if not isinstance(raw_settings, dict):
        return None
    metadata: dict[str, str] = {}
    for key, value in raw_settings.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        metadata[key] = value
    return IndexingSettings.from_metadata(metadata)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object) -> int | None:
    match value:
        case bool():
            return None
        case int() if value >= 0:
            return value
        case float() if value.is_integer() and value >= 0:
            return int(value)
        case str() if value.strip().isdigit():
            return int(value.strip())
    return None


def _refresh_job(value: object) -> _RefreshJob:
    if value in _REFRESH_JOBS:
        return cast("_RefreshJob", value)
    return "idle"
