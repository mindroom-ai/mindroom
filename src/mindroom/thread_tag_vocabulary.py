"""Daily-rebuilt snapshot of thread-tag usage for cache-stable tool descriptions.

The `tag_thread` tool description embeds the most-used tags so agents converge
on a shared vocabulary. Tool definitions sit at the front of the prompt-cache
prefix, so the ranked list is rebuilt at most once per day at a fixed
early-morning boundary instead of on every tag change.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.thread_tags import ThreadTagsError, list_tagged_threads

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_VOCABULARY_DESCRIPTION_TAG_LIMIT = 20
_REBUILD_BOUNDARY_HOUR = 4
_VOCABULARY_FILENAME = "thread_tag_vocabulary.json"
_SNAPSHOT_VERSION = 1

# Boundary of the last confirmed-fresh snapshot, kept in memory so the
# per-response pre-queue check never touches the filesystem.
_last_confirmed_fresh_boundary: datetime | None = None
_rebuild_lock = asyncio.Lock()


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


def _snapshot_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _VOCABULARY_FILENAME


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
    if not isinstance(tag, str) or not isinstance(count, int):
        return None
    return _TagUsage(tag=tag, count=count)


def _parse_snapshot_payload(payload: object) -> _TagVocabularySnapshot | None:
    """Parse one persisted snapshot payload, treating malformed data as absent."""
    if not isinstance(payload, Mapping):
        return None
    typed_payload = cast("Mapping[str, object]", payload)

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
        usages.append(usage)

    return _TagVocabularySnapshot(built_at=built_at, tags=tuple(usages))


def load_tag_vocabulary_snapshot(runtime_paths: RuntimePaths) -> _TagVocabularySnapshot | None:
    """Load the persisted vocabulary snapshot, or None when absent or malformed."""
    path = _snapshot_path(runtime_paths)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    return _parse_snapshot_payload(payload)


def _most_recent_rebuild_boundary(now: datetime, timezone_name: str) -> datetime:
    """Return the most recent daily rebuild boundary at or before *now*."""
    local_now = now.astimezone(ZoneInfo(timezone_name))
    boundary = local_now.replace(hour=_REBUILD_BOUNDARY_HOUR, minute=0, second=0, microsecond=0)
    if local_now < boundary:
        boundary -= timedelta(days=1)
    return boundary


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


def vocabulary_check_due(config: Config, *, now: datetime) -> bool:
    """Return whether a background freshness check should be queued.

    Purely in-memory so the per-response pre-queue gate stays free of file IO;
    the queued background task performs the authoritative on-disk check.
    """
    if _last_confirmed_fresh_boundary is None:
        return True
    return _last_confirmed_fresh_boundary < _most_recent_rebuild_boundary(now, config.timezone)


async def _count_tag_usage(client: nio.AsyncClient) -> Counter[str]:
    """Count tag usage across threads in every joined room."""
    usage: Counter[str] = Counter()
    for room_id in sorted(client.rooms):
        try:
            listing = await list_tagged_threads(client, room_id)
        except ThreadTagsError as exc:
            logger.warning(
                "Skipping room during tag vocabulary aggregation",
                room_id=room_id,
                error=str(exc),
            )
            continue
        for state in listing.tag_state.values():
            usage.update(state.tags.keys())
    return usage


def _ranked_tag_usage(usage: Counter[str]) -> tuple[_TagUsage, ...]:
    """Rank tags by usage count descending, tie-broken alphabetically."""
    ranked = sorted(usage.items(), key=lambda item: (-item[1], item[0]))
    return tuple(_TagUsage(tag=tag, count=count) for tag, count in ranked)


def _write_snapshot(runtime_paths: RuntimePaths, snapshot: _TagVocabularySnapshot) -> None:
    """Persist one snapshot durably."""
    payload = {
        "version": _SNAPSHOT_VERSION,
        "built_at": snapshot.built_at.isoformat(),
        "tags": [{"tag": usage.tag, "count": usage.count} for usage in snapshot.tags],
    }
    write_json_file_durable(_snapshot_path(runtime_paths), payload, indent=2, trailing_newline=True)


async def maybe_rebuild_tag_vocabulary(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    now: datetime,
) -> bool:
    """Rebuild the vocabulary snapshot when it predates the daily boundary.

    Returns whether a rebuild happened. Serialized so concurrent post-response
    tasks trigger at most one aggregation pass.
    """
    global _last_confirmed_fresh_boundary
    async with _rebuild_lock:
        boundary = _most_recent_rebuild_boundary(now, config.timezone)
        if _last_confirmed_fresh_boundary is not None and _last_confirmed_fresh_boundary >= boundary:
            return False

        snapshot = load_tag_vocabulary_snapshot(runtime_paths)
        if not _snapshot_is_stale(snapshot, now=now, timezone_name=config.timezone):
            _last_confirmed_fresh_boundary = boundary
            return False

        usage = await _count_tag_usage(client)
        rebuilt = _TagVocabularySnapshot(built_at=now, tags=_ranked_tag_usage(usage))
        _write_snapshot(runtime_paths, rebuilt)
        _last_confirmed_fresh_boundary = boundary
        logger.info(
            "Rebuilt thread tag vocabulary snapshot",
            tag_count=len(rebuilt.tags),
            top_tags=[usage.tag for usage in rebuilt.tags[:_VOCABULARY_DESCRIPTION_TAG_LIMIT]],
        )
        return True


def format_tag_vocabulary_for_description(snapshot: _TagVocabularySnapshot | None) -> str:
    """Format the ranked tag list (without counts) for the tag_thread tool description."""
    if snapshot is None or not snapshot.tags:
        return "No tags are in use yet; coin sensible new ones."
    ranked_tags = ", ".join(usage.tag for usage in snapshot.tags[:_VOCABULARY_DESCRIPTION_TAG_LIMIT])
    return f"Most-used tags, ranked (this list is rebuilt once a day): {ranked_tags}"


def format_tag_vocabulary_with_counts(snapshot: _TagVocabularySnapshot | None) -> str:
    """Format the ranked tag list with usage counts for the auto-tagger prompt."""
    if snapshot is None or not snapshot.tags:
        return "(no tags in use yet)"
    return "\n".join(f"- {usage.tag} ({usage.count})" for usage in snapshot.tags[:_VOCABULARY_DESCRIPTION_TAG_LIMIT])
