"""Bounded mapping shared by the process-global knowledge caches.

Every knowledge cache is keyed by something whose cardinality the runtime does not
control -- a per-requester private binding, a knowledge source root, a refresh
cooldown fingerprint -- so each one needs the same "evict the oldest entries once
we exceed a cap" rule plus the same "prune after every insert" discipline. Sharing
one implementation keeps that rule in a single place instead of once per cache.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import TypeVar

_K = TypeVar("_K")
_V = TypeVar("_V")


def _track_every_entry(_key: object, _value: object) -> bool:
    return True


def _pin_no_entry(_key: object, _value: object) -> bool:
    return False


@dataclass(eq=False)
class BoundedMap(MutableMapping[_K, _V]):
    """Insertion-ordered mapping that evicts its oldest tracked entries past ``capacity``.

    ``tracked`` selects which entries ``capacity`` applies to, so a cache can bound
    only the subset of keys with unbounded cardinality and leave the rest alone.
    ``pinned`` protects entries that must survive eviction while they are in use;
    pinned entries still consume capacity, so the map may exceed ``capacity`` while
    they are held. ``eviction_order`` replaces insertion order when a different
    notion of "oldest" applies, such as a stored timestamp.

    Not thread-safe: callers that mutate from several threads hold their own lock.
    """

    capacity: int
    tracked: Callable[[_K, _V], bool] = _track_every_entry
    pinned: Callable[[_K, _V], bool] = _pin_no_entry
    eviction_order: Callable[[_K, _V], float] | None = None
    _entries: dict[_K, _V] = field(default_factory=dict)

    def __getitem__(self, key: _K) -> _V:
        """Return the entry stored under ``key``."""
        return self._entries[key]

    def __setitem__(self, key: _K, value: _V) -> None:
        """Store an entry, then evict so no caller has to remember to prune."""
        self._entries[key] = value
        self.prune()

    def __delitem__(self, key: _K) -> None:
        """Drop the entry stored under ``key``."""
        del self._entries[key]

    def __iter__(self) -> Iterator[_K]:
        """Iterate keys in insertion order."""
        return iter(self._entries)

    def __len__(self) -> int:
        """Return how many entries the map currently holds."""
        return len(self._entries)

    def prune(self) -> None:
        """Drop the oldest unpinned tracked entries until tracked entries fit ``capacity``."""
        tracked = [(key, value) for key, value in self._entries.items() if self.tracked(key, value)]
        excess = len(tracked) - self.capacity
        if excess <= 0:
            return
        eviction_order = self.eviction_order
        if eviction_order is not None:
            tracked.sort(key=lambda entry: eviction_order(entry[0], entry[1]))
        for key, value in tracked:
            if excess <= 0:
                return
            if self.pinned(key, value):
                continue
            del self._entries[key]
            excess -= 1
