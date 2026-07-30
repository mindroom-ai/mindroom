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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast, get_args

from mindroom.knowledge.indexing_config import IndexingSettings

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


_PublishedIndexStatus = Literal["resetting", "indexing", "complete", "failed"]
_RefreshJob = Literal["idle", "pending", "running", "failed"]

# Derived, never restated: a hand-written copy that drifts from the Literal
# makes the loader silently refuse every record carrying the new value, or
# makes the cast that follows the membership test a lie.
_PUBLISHED_INDEX_STATUSES: frozenset[str] = frozenset(get_args(_PublishedIndexStatus))
_REFRESH_JOBS: frozenset[str] = frozenset(get_args(_RefreshJob))


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
    # A JSON array or object decodes to an unhashable value, so membership is
    # only asked once the value is known to be a string.
    if settings is None or not isinstance(status, str) or status not in _PUBLISHED_INDEX_STATUSES:
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


def state_for_publication(
    *,
    settings: IndexingSettings,
    collection: str | None,
    indexed_count: int,
    source_signature: str,
    published_revision: str | None,
) -> PublishedIndexState:
    """Return the whole state one successful publication leaves on disk.

    Both publish paths -- the semantic one that swaps in a candidate
    collection, and the file-mode one that publishes source metadata with no
    collection at all -- differ only in those two values, so they share this.
    Building the state twice is how a field gets left to its default by
    omission, which is the failure this module exists to make impossible.

    Publication also resolves the refresh job it belongs to: the work that job
    tracked has just landed, so its reason, error and failure streak are
    cleared by the same write rather than surviving into a ``complete`` record.
    """
    now = datetime.now(tz=UTC).isoformat()
    return PublishedIndexState(
        settings=settings,
        status="complete",
        collection=collection,
        last_published_at=now,
        published_revision=published_revision,
        indexed_count=indexed_count,
        source_signature=source_signature,
        refresh_job="idle",
        reason=None,
        last_error=None,
        updated_at=now,
        last_refresh_at=now,
        consecutive_refresh_failures=0,
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
    # ``ValueError`` rather than ``JSONDecodeError``: invalid UTF-8 bytes raise
    # ``UnicodeDecodeError``, which is a ``ValueError`` and not an ``OSError``,
    # and every caller of the loader treats an unreadable file as "no state".
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else None
    except (OSError, ValueError):
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
        # ``is_integer`` is false for both infinities and NaN, which are the
        # only floats ``int`` refuses, and no finite float can exceed its
        # reach: this conversion cannot raise.
        case float() if value.is_integer() and value >= 0:
            return int(value)
        case str():
            return _nonnegative_int_from_text(value.strip())
    return None


def _nonnegative_int_from_text(text: str) -> int | None:
    """Parse a decimal count, treating anything ``int`` refuses as absent.

    Catching the failure beats predicating on it. No property of the
    characters decides whether ``int`` accepts them: ``isdigit`` admits
    superscripts like "²" that it rejects, and even a well-formed all-ASCII
    decimal string raises once it passes CPython's integer conversion limit,
    which is not about characters at all. A caller of this loader treats it as
    total, so one ``except`` covering both -- and whatever else ``int`` grows
    -- is what makes that true. ``isdecimal`` still gates which shapes count
    as a number, keeping "+1" and "1_000" out; it just no longer decides
    whether converting is safe.
    """
    if not text.isdecimal():
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _refresh_job(value: object) -> _RefreshJob:
    if isinstance(value, str) and value in _REFRESH_JOBS:
        return cast("_RefreshJob", value)
    return "idle"
