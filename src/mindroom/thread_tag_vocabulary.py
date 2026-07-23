"""Daily room-scoped snapshots of thread-tag usage for cache-stable prompts.

The `tag_thread` tool description embeds the most-used short tags for the
current room so agents converge on a shared vocabulary without leaking tag
names between rooms. The ranked list is rebuilt at most once per day at local
midnight instead of on every tag change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter, OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.thread_tags import AUTOMATIC_THREAD_TAG_EXCLUSIONS, COERCED_TAG_MAX_LENGTH, list_tagged_threads

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_VOCABULARY_DESCRIPTION_TAG_LIMIT = 20
_REBUILD_BOUNDARY_HOUR = 0
_VOCABULARY_DIRECTORY = "thread_tag_vocabulary"
_SNAPSHOT_VERSION = 1
_REBUILD_FAILURE_RETRY_DELAY = timedelta(minutes=5)
_MAX_TRACKED_ROOM_SCOPES = 2048

type _VocabularyScopeKey = tuple[Path, str]

# These in-memory maps keep the per-response pre-queue check free of file IO.
# Reservations prevent unrelated thread responses from creating a daily rebuild herd.
_last_confirmed_fresh_boundaries: OrderedDict[_VocabularyScopeKey, datetime] = OrderedDict()
_reserved_rebuild_boundaries: OrderedDict[_VocabularyScopeKey, datetime] = OrderedDict()
_rebuild_retry_not_before: OrderedDict[_VocabularyScopeKey, datetime] = OrderedDict()
_rebuild_locks: WeakValueDictionary[_VocabularyScopeKey, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class _TagUsage:
    """One tag with its usage count across threads."""

    tag: str
    count: int


@dataclass(frozen=True, slots=True)
class _TagVocabularySnapshot:
    """Ranked tag-usage snapshot with its build timestamp."""

    built_at: datetime
    tags: tuple[_TagUsage, ...]


def _scope_key(runtime_paths: RuntimePaths, room_id: str) -> _VocabularyScopeKey:
    return runtime_paths.storage_root, room_id


def _remember_boundary(
    boundaries: OrderedDict[_VocabularyScopeKey, datetime],
    scope_key: _VocabularyScopeKey,
    boundary: datetime,
) -> None:
    """Remember one room boundary while bounding process-lifetime state."""
    boundaries[scope_key] = boundary
    boundaries.move_to_end(scope_key)
    while len(boundaries) > _MAX_TRACKED_ROOM_SCOPES:
        boundaries.popitem(last=False)


def _rebuild_lock(scope_key: _VocabularyScopeKey) -> asyncio.Lock:
    """Return the shared live rebuild lock for one room scope."""
    lock = _rebuild_locks.get(scope_key)
    if lock is None:
        lock = asyncio.Lock()
        _rebuild_locks[scope_key] = lock
    return lock


def _snapshot_path(runtime_paths: RuntimePaths, room_id: str) -> Path:
    room_digest = hashlib.sha256(room_id.encode()).hexdigest()
    return tracking_dir(runtime_paths) / _VOCABULARY_DIRECTORY / f"{room_digest}.json"


def _parse_built_at(value: object) -> datetime | None:
    """Parse one aware build timestamp."""
    if not isinstance(value, str):
        return None
    try:
        built_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    if built_at.tzinfo is None:
        return None
    return built_at


def _parse_tag_usage_entry(entry: object) -> _TagUsage | None:
    """Parse one persisted tag-usage entry."""
    if not isinstance(entry, Mapping):
        return None
    typed_entry = cast("Mapping[str, object]", entry)
    tag = typed_entry.get("tag")
    count = typed_entry.get("count")
    if not isinstance(tag, str) or not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return None
    return _TagUsage(tag=tag, count=count)


def _parse_snapshot_payload(payload: object, *, room_id: str) -> _TagVocabularySnapshot | None:
    """Parse one persisted snapshot payload, treating malformed data as absent."""
    if not isinstance(payload, Mapping):
        return None
    typed_payload = cast("Mapping[str, object]", payload)
    if typed_payload.get("version") != _SNAPSHOT_VERSION or typed_payload.get("room_id") != room_id:
        return None

    built_at = _parse_built_at(typed_payload.get("built_at"))
    if built_at is None:
        return None

    raw_tags = typed_payload.get("tags")
    if not isinstance(raw_tags, list):
        return None
    usages: list[_TagUsage] = []
    for entry in raw_tags:
        usage = _parse_tag_usage_entry(entry)
        if usage is None:
            return None
        if usage.tag not in AUTOMATIC_THREAD_TAG_EXCLUSIONS:
            usages.append(usage)

    return _TagVocabularySnapshot(built_at=built_at, tags=tuple(usages))


def load_tag_vocabulary_snapshot(
    runtime_paths: RuntimePaths,
    room_id: str,
) -> _TagVocabularySnapshot | None:
    """Load one room's vocabulary snapshot, or None when absent or malformed."""
    path = _snapshot_path(runtime_paths, room_id)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    return _parse_snapshot_payload(payload, room_id=room_id)


def _most_recent_rebuild_boundary(now: datetime, timezone_name: str) -> datetime:
    """Return the most recent local-midnight rebuild boundary."""
    local_now = now.astimezone(ZoneInfo(timezone_name))
    return local_now.replace(hour=_REBUILD_BOUNDARY_HOUR, minute=0, second=0, microsecond=0)


def _snapshot_is_stale(
    snapshot: _TagVocabularySnapshot | None,
    *,
    now: datetime,
    timezone_name: str,
) -> bool:
    """Return whether the snapshot predates the most recent daily boundary."""
    if snapshot is None:
        return True
    return snapshot.built_at < _most_recent_rebuild_boundary(now, timezone_name)


def claim_vocabulary_check(
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    now: datetime,
) -> bool:
    """Claim one room's due background freshness check without file IO."""
    scope_key = _scope_key(runtime_paths, room_id)
    boundary = _most_recent_rebuild_boundary(now, config.timezone)
    confirmed_boundary = _last_confirmed_fresh_boundaries.get(scope_key)
    if confirmed_boundary is not None and confirmed_boundary >= boundary:
        return False

    retry_not_before = _rebuild_retry_not_before.get(scope_key)
    if retry_not_before is not None:
        if retry_not_before > now:
            return False
        _rebuild_retry_not_before.pop(scope_key, None)

    reserved_boundary = _reserved_rebuild_boundaries.get(scope_key)
    if reserved_boundary is not None and reserved_boundary >= boundary:
        return False
    _remember_boundary(_reserved_rebuild_boundaries, scope_key, boundary)
    return True


def _release_rebuild_claim(scope_key: _VocabularyScopeKey, boundary: datetime) -> None:
    reserved_boundary = _reserved_rebuild_boundaries.get(scope_key)
    if reserved_boundary is not None and reserved_boundary <= boundary:
        _reserved_rebuild_boundaries.pop(scope_key, None)


async def _count_tag_usage(client: nio.AsyncClient, room_id: str) -> Counter[str]:
    """Count tag usage across threads in one room."""
    listing = await list_tagged_threads(client, room_id)
    usage: Counter[str] = Counter()
    for state in listing.tag_state.values():
        usage.update(state.tags.keys())
    return usage


def _ranked_tag_usage(usage: Counter[str]) -> tuple[_TagUsage, ...]:
    """Rank model-reproducible tags by usage, tie-broken alphabetically."""
    ranked = sorted(
        (
            (tag, count)
            for tag, count in usage.items()
            if len(tag) <= COERCED_TAG_MAX_LENGTH and tag not in AUTOMATIC_THREAD_TAG_EXCLUSIONS
        ),
        key=lambda item: (-item[1], item[0]),
    )
    return tuple(_TagUsage(tag=tag, count=count) for tag, count in ranked[:_VOCABULARY_DESCRIPTION_TAG_LIMIT])


def _write_snapshot(
    runtime_paths: RuntimePaths,
    room_id: str,
    snapshot: _TagVocabularySnapshot,
) -> None:
    """Persist one room snapshot durably."""
    payload = {
        "version": _SNAPSHOT_VERSION,
        "room_id": room_id,
        "built_at": snapshot.built_at.isoformat(),
        "tags": [{"tag": usage.tag, "count": usage.count} for usage in snapshot.tags],
    }
    write_json_file_durable(
        _snapshot_path(runtime_paths, room_id),
        payload,
        indent=2,
        trailing_newline=True,
    )


async def maybe_rebuild_tag_vocabulary(
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    now: datetime,
) -> _TagVocabularySnapshot | None:
    """Return a snapshot when this check reads or rebuilds one."""
    scope_key = _scope_key(runtime_paths, room_id)
    boundary = _most_recent_rebuild_boundary(now, config.timezone)
    try:
        async with _rebuild_lock(scope_key):
            confirmed_boundary = _last_confirmed_fresh_boundaries.get(scope_key)
            if confirmed_boundary is not None and confirmed_boundary >= boundary:
                return None
            retry_not_before = _rebuild_retry_not_before.get(scope_key)
            if retry_not_before is not None and retry_not_before > now:
                return None

            snapshot = load_tag_vocabulary_snapshot(runtime_paths, room_id)
            if not _snapshot_is_stale(snapshot, now=now, timezone_name=config.timezone):
                _remember_boundary(_last_confirmed_fresh_boundaries, scope_key, boundary)
                _rebuild_retry_not_before.pop(scope_key, None)
                return snapshot

            usage = await _count_tag_usage(client, room_id)
            rebuilt = _TagVocabularySnapshot(built_at=now, tags=_ranked_tag_usage(usage))
            _write_snapshot(runtime_paths, room_id, rebuilt)
            _remember_boundary(_last_confirmed_fresh_boundaries, scope_key, boundary)
            _rebuild_retry_not_before.pop(scope_key, None)
            logger.info(
                "Rebuilt thread tag vocabulary snapshot",
                room_id=room_id,
                tag_count=len(rebuilt.tags),
                top_tags=[usage.tag for usage in rebuilt.tags],
            )
            return rebuilt
    except Exception:
        _remember_boundary(
            _rebuild_retry_not_before,
            scope_key,
            now + _REBUILD_FAILURE_RETRY_DELAY,
        )
        raise
    finally:
        _release_rebuild_claim(scope_key, boundary)


def format_tag_vocabulary_for_description(snapshot: _TagVocabularySnapshot | None) -> str:
    """Format the ranked tag list without counts for the tag_thread description."""
    if snapshot is None or not snapshot.tags:
        return "No reusable short tags are in use yet; coin sensible new ones."
    ranked_tags = ", ".join(usage.tag for usage in snapshot.tags[:_VOCABULARY_DESCRIPTION_TAG_LIMIT])
    return f"Most-used short tags in this room, ranked (rebuilt once a day): {ranked_tags}"


def format_tag_vocabulary_with_counts(snapshot: _TagVocabularySnapshot | None) -> str:
    """Format the ranked tag list with usage counts for initial enrichment."""
    if snapshot is None or not snapshot.tags:
        return "(no reusable short tags in use yet)"
    return "\n".join(f"- {usage.tag} ({usage.count})" for usage in snapshot.tags[:_VOCABULARY_DESCRIPTION_TAG_LIMIT])
