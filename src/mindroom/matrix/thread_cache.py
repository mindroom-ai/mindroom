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
class ThreadFreshnessState:
    """Mutable freshness state for one thread."""

    generation: int = 0
    repair_required: bool = False
    pending_lookup_event_ids: set[str] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass(slots=True)
class ResolvedThreadCache:
    """Bounded LRU cache of resolved histories plus per-thread freshness state."""

    max_entries: int = 200
    ttl_seconds: float = 300.0
    _entries: OrderedDict[ThreadCacheKey, ResolvedThreadCacheEntry] = field(default_factory=OrderedDict, init=False)
    _states: dict[ThreadCacheKey, ThreadFreshnessState] = field(default_factory=dict, init=False)
    _next_generation: int = field(default=1, init=False)

    def _state(self, key: ThreadCacheKey) -> ThreadFreshnessState:
        return self._states.setdefault(key, ThreadFreshnessState())

    def version(self, room_id: str, thread_id: str) -> int:
        """Return the current in-memory generation for one thread."""
        return self._state((room_id, thread_id)).generation

    def bump_version(self, room_id: str, thread_id: str) -> int:
        """Advance one thread generation without reusing tokens during this process."""
        state = self._state((room_id, thread_id))
        generation = self._next_generation
        self._next_generation += 1
        state.generation = generation
        return generation

    def repair_required(self, room_id: str, thread_id: str) -> bool:
        """Return whether one thread requires authoritative repair."""
        return self._state((room_id, thread_id)).repair_required

    def mark_repair_required(self, room_id: str, thread_id: str) -> None:
        """Mark one thread as requiring authoritative repair."""
        self._state((room_id, thread_id)).repair_required = True

    def clear_repair_required(self, room_id: str, thread_id: str) -> None:
        """Clear the repair-required flag for one thread."""
        self._state((room_id, thread_id)).repair_required = False

    def pending_lookup_repairs(self, room_id: str, thread_id: str) -> frozenset[str]:
        """Return promoted lookup-failure event IDs for one thread."""
        return frozenset(self._state((room_id, thread_id)).pending_lookup_event_ids)

    def mark_pending_lookup_repairs(
        self,
        room_id: str,
        thread_id: str,
        event_ids: frozenset[str],
    ) -> None:
        """Promote lookup-failure candidates into one concrete thread."""
        if not event_ids:
            return
        self._state((room_id, thread_id)).pending_lookup_event_ids.update(event_ids)

    def clear_pending_lookup_repairs(self, room_id: str, thread_id: str) -> None:
        """Clear promoted lookup-failure event IDs for one thread."""
        self._state((room_id, thread_id)).pending_lookup_event_ids.clear()

    def lookup(self, room_id: str, thread_id: str) -> ResolvedThreadCacheLookup:
        """Return one entry when still fresh, evicting expired entries eagerly."""
        key = (room_id, thread_id)
        entry = self._entries.get(key)
        if entry is None:
            return ResolvedThreadCacheLookup(entry=None)
        if self._is_expired(entry):
            self._entries.pop(key, None)
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
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def invalidate(self, room_id: str, thread_id: str) -> ResolvedThreadCacheEntry | None:
        """Drop one cache entry if it exists."""
        key = (room_id, thread_id)
        return self._entries.pop(key, None)

    def _is_expired(self, entry: ResolvedThreadCacheEntry) -> bool:
        return (time.monotonic() - entry.cached_at_monotonic) >= self.ttl_seconds

    @asynccontextmanager
    async def entry_lock(self, room_id: str, thread_id: str) -> AsyncIterator[None]:
        """Serialize concurrent reads and writes for one thread freshness state."""
        lock = self._state((room_id, thread_id)).lock
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()


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
