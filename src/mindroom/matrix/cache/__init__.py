"""Matrix cache domain ownership.

Developer note:
- `event_cache.py` owns the public SQLite cache boundary plus runtime, locking, and schema lifecycle.
- `event_cache_events.py` owns event lookup normalization, lookup/index rows, edits, and redaction tombstones.
- `event_cache_threads.py` owns thread snapshot rows, cache-state reads, and thread/room invalidation state.
- `thread_writes.py` owns live, outbound, and sync mutation flows; `thread_bookkeeping.py` resolves thread impact and `thread_write_cache_ops.py` applies queued cache mutations.

Public boundary:
- `_EventCache` in `event_cache.py` is the durable cache API used by conversation and client code.
- `mindroom.matrix.cache.normalize_nio_event_for_cache` is the package-level normalization helper for callers above the cache package.
- `MatrixConversationCache` remains the higher-level conversation read/write facade above it.

Main invariants:
- Runtime disable and room/db ordering live only in `event_cache.py`.
- Event lookup rows and thread snapshot rows are written together so lookup, edit, and thread indexes stay consistent.
- Thread invalidation is durable state first, with fail-closed deletion only when stale markers cannot be written.
"""

from .event_cache_events import normalize_nio_event_for_cache

__all__ = ["normalize_nio_event_for_cache"]
