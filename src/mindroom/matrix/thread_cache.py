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
    """Bounded LRU cache of resolved histories plus process-local thread freshness generations."""

    max_entries: int = 200
    ttl_seconds: float = 300.0
    generation_retention_seconds: float = 300.0
    _entries: OrderedDict[ThreadCacheKey, ResolvedThreadCacheEntry] = field(default_factory=OrderedDict, init=False)
    _generations: dict[ThreadCacheKey, int] = field(default_factory=dict, init=False)
    _generation_touched_at: dict[ThreadCacheKey, float] = field(default_factory=dict, init=False, repr=False)
    _locks: dict[ThreadCacheKey, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)
    _next_generation: int = field(default=1, init=False)

    def _ensure_lock(self, key: ThreadCacheKey) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _prune_lock(self, key: ThreadCacheKey) -> None:
        lock = self._locks.get(key)
        if key in self._entries or (lock is not None and lock.locked()):
            return
        self._locks.pop(key, None)

    def _touch_generation(self, key: ThreadCacheKey, generation: int) -> int:
        self._generations[key] = generation
        self._generation_touched_at[key] = time.monotonic()
        return generation

    def _prune_stale_generations(self) -> None:
        if not self._generation_touched_at:
            return
        cutoff = time.monotonic() - self.generation_retention_seconds
        for key, touched_at in tuple(self._generation_touched_at.items()):
            if touched_at > cutoff:
                continue
            lock = self._locks.get(key)
            if key in self._entries or (lock is not None and lock.locked()):
                continue
            self._generation_touched_at.pop(key, None)
            self._generations.pop(key, None)
            self._prune_lock(key)

    def version(self, room_id: str, thread_id: str) -> int:
        """Return the current in-memory generation for one thread."""
        self._prune_stale_generations()
        return self._generations.get((room_id, thread_id), 0)

    def bump_version(self, room_id: str, thread_id: str) -> int:
        """Advance one thread generation without reusing tokens during this process."""
        generation = self._next_generation
        self._next_generation += 1
        self._prune_stale_generations()
        return self._touch_generation((room_id, thread_id), generation)

    def lookup(self, room_id: str, thread_id: str) -> ResolvedThreadCacheLookup:
        """Return one entry when still fresh, evicting expired entries eagerly."""
        key = (room_id, thread_id)
        entry = self._entries.get(key)
        if entry is None:
            return ResolvedThreadCacheLookup(entry=None)
        if self._is_expired(entry):
            self._entries.pop(key, None)
            self._prune_lock(key)
            self._prune_stale_generations()
            return ResolvedThreadCacheLookup(entry=None, expired=True)
        self._entries.move_to_end(key)
        return ResolvedThreadCacheLookup(entry=entry)

    def matching_thread_ids(self, room_id: str, event_ids: frozenset[str]) -> tuple[str, ...]:
        """Return cached thread IDs whose source-event set intersects the provided event IDs."""
        if not event_ids:
            return ()
        return tuple(
            thread_id
            for (candidate_room_id, thread_id), entry in tuple(self._entries.items())
            if candidate_room_id == room_id and not entry.source_event_ids.isdisjoint(event_ids)
        )

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
        self._touch_generation(key, max(self._generations.get(key, 0), entry.thread_version))
        while len(self._entries) > self.max_entries:
            evicted_key, _evicted_entry = self._entries.popitem(last=False)
            self._prune_lock(evicted_key)
        self._prune_stale_generations()

    def invalidate(self, room_id: str, thread_id: str) -> ResolvedThreadCacheEntry | None:
        """Drop one cache entry if it exists."""
        key = (room_id, thread_id)
        entry = self._entries.pop(key, None)
        self._prune_lock(key)
        self._prune_stale_generations()
        return entry

    def _is_expired(self, entry: ResolvedThreadCacheEntry) -> bool:
        return (time.monotonic() - entry.cached_at_monotonic) >= self.ttl_seconds

    @asynccontextmanager
    async def entry_lock(self, room_id: str, thread_id: str) -> AsyncIterator[None]:
        """Serialize concurrent reads and writes for one thread freshness state."""
        key = (room_id, thread_id)
        lock = self._ensure_lock(key)
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            self._prune_lock(key)
            self._prune_stale_generations()

    def clear(self) -> None:
        """Drop all cached resolved histories and freshness metadata."""
        self._entries.clear()
        self._generations.clear()
        self._generation_touched_at.clear()
        self._locks.clear()


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
