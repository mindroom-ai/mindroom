"""In-memory resolved thread-history cache for cross-turn reuse."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from mindroom.matrix.client import ResolvedVisibleMessage


type ThreadCacheKey = tuple[str, str]


def _clone_history(history: Sequence[ResolvedVisibleMessage]) -> list[ResolvedVisibleMessage]:
    return [
        replace(
            message,
            content=dict(message.content),
        )
        for message in history
    ]


@dataclass(slots=True)
class ResolvedThreadCacheEntry:
    """One cached resolved thread plus the source-event state it was built from."""

    history: list[ResolvedVisibleMessage]
    source_event_ids: frozenset[str]
    thread_version: int
    cached_at_monotonic: float

    def clone_history(self) -> list[ResolvedVisibleMessage]:
        """Return a detached copy so callers cannot mutate the cached entry in place."""
        return _clone_history(self.history)


@dataclass(slots=True)
class ResolvedThreadCacheLookup:
    """Describe one cache lookup result, including whether TTL eviction occurred."""

    entry: ResolvedThreadCacheEntry | None
    expired: bool = False


@dataclass(slots=True)
class ResolvedThreadCache:
    """Bounded LRU cache of fully resolved thread histories and their generations."""

    max_entries: int = 200
    ttl_seconds: float = 300.0
    max_tracked_versions: int = 200
    _entries: OrderedDict[ThreadCacheKey, ResolvedThreadCacheEntry] = field(default_factory=OrderedDict, init=False)
    _versions: OrderedDict[ThreadCacheKey, int] = field(default_factory=OrderedDict, init=False)
    _locks: dict[ThreadCacheKey, asyncio.Lock] = field(default_factory=dict, init=False)

    def _prune_lock_if_idle(self, key: ThreadCacheKey) -> None:
        lock = self._locks.get(key)
        if lock is not None and not lock.locked():
            self._locks.pop(key, None)

    def version(self, room_id: str, thread_id: str) -> int:
        """Return the current in-memory generation for one thread."""
        key = (room_id, thread_id)
        version = self._versions.get(key)
        if version is None:
            return 0
        self._versions.move_to_end(key)
        return version

    def bump_version(self, room_id: str, thread_id: str) -> int:
        """Advance one thread generation and keep the generation index bounded."""
        key = (room_id, thread_id)
        next_version = self._versions.get(key, 0) + 1
        self._store_version(key, next_version)
        return next_version

    def _store_version(self, key: ThreadCacheKey, version: int) -> None:
        self._versions[key] = version
        self._versions.move_to_end(key)
        while len(self._versions) > self.max_tracked_versions:
            evicted_key, _evicted_version = self._versions.popitem(last=False)
            self._entries.pop(evicted_key, None)
            self._prune_lock_if_idle(evicted_key)

    def lookup(self, room_id: str, thread_id: str) -> ResolvedThreadCacheLookup:
        """Return one entry when still fresh, evicting expired entries eagerly."""
        key = (room_id, thread_id)
        entry = self._entries.get(key)
        if entry is None:
            return ResolvedThreadCacheLookup(entry=None)
        if self._is_expired(entry):
            self._entries.pop(key, None)
            self._prune_lock_if_idle(key)
            return ResolvedThreadCacheLookup(entry=None, expired=True)
        self._entries.move_to_end(key)
        return ResolvedThreadCacheLookup(entry=entry)

    def store(
        self,
        room_id: str,
        thread_id: str,
        entry: ResolvedThreadCacheEntry,
    ) -> None:
        """Insert or replace one cache entry and enforce the LRU bound."""
        key = (room_id, thread_id)
        self._entries[key] = entry
        self._entries.move_to_end(key)
        self._store_version(key, entry.thread_version)
        while len(self._entries) > self.max_entries:
            evicted_key, _evicted_entry = self._entries.popitem(last=False)
            self._versions.pop(evicted_key, None)
            self._prune_lock_if_idle(evicted_key)

    def invalidate(self, room_id: str, thread_id: str) -> ResolvedThreadCacheEntry | None:
        """Drop one cache entry if it exists."""
        key = (room_id, thread_id)
        entry = self._entries.pop(key, None)
        self._prune_lock_if_idle(key)
        return entry

    def _is_expired(self, entry: ResolvedThreadCacheEntry) -> bool:
        return (time.monotonic() - entry.cached_at_monotonic) >= self.ttl_seconds

    @asynccontextmanager
    async def entry_lock(self, room_id: str, thread_id: str) -> AsyncIterator[None]:
        """Serialize concurrent fills and refreshes for one thread cache entry."""
        key = (room_id, thread_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            if key not in self._entries:
                self._prune_lock_if_idle(key)


def resolved_thread_cache_entry(
    *,
    history: Sequence[ResolvedVisibleMessage],
    source_event_ids: frozenset[str],
    thread_version: int,
) -> ResolvedThreadCacheEntry:
    """Construct a cache entry with detached message state."""
    return ResolvedThreadCacheEntry(
        history=_clone_history(history),
        source_event_ids=source_event_ids,
        thread_version=thread_version,
        cached_at_monotonic=time.monotonic(),
    )
