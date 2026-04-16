"""Matrix cache domain ownership.

Developer note:
- `event_cache.py` owns the concrete durable cache implementation, runtime, locking, and schema lifecycle.
- `event_cache_events.py` owns event lookup normalization, lookup/index rows, edits, and redaction tombstones.
- `event_cache_threads.py` owns thread snapshot rows, cache-state reads, and thread/room invalidation state.
- `MatrixConversationCache` owns its private read and write helper logic above the cache package, while `thread_bookkeeping.py` remains the shared thread-impact domain.

Package boundary:
- `mindroom.matrix.cache` is the package-level import surface for cache-facing contracts and shared helpers used above the cache package.
- `_EventCache` and `_EventCacheWriteCoordinator` remain private concrete implementations used by `runtime_support.py` as the composition-root exception.
- `MatrixConversationCache` remains the higher-level conversation read/write facade above the cache package, including the private read and write wrapper logic that used to be exported here.

Main invariants:
- Runtime disable and room/db ordering live only in the concrete event-cache implementation.
- Event lookup rows and thread snapshot rows are written together so lookup, edit, and thread indexes stay consistent.
- Thread invalidation is durable state first, with fail-closed deletion only when stale markers cannot be written.
"""

from .event_cache import ConversationEventCache, ThreadCacheState, _EventCache
from .event_cache_events import normalize_nio_event_for_cache
from .thread_cache_helpers import thread_cache_state_is_usable
from .thread_history_result import (
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
    ThreadHistoryResult,
    thread_history_result,
)
from .write_coordinator import EventCacheWriteCoordinator, _EventCacheWriteCoordinator

__all__ = [
    "THREAD_HISTORY_DEGRADED_DIAGNOSTIC",
    "THREAD_HISTORY_ERROR_DIAGNOSTIC",
    "THREAD_HISTORY_SOURCE_CACHE",
    "THREAD_HISTORY_SOURCE_DIAGNOSTIC",
    "THREAD_HISTORY_SOURCE_HOMESERVER",
    "THREAD_HISTORY_SOURCE_STALE_CACHE",
    "ConversationEventCache",
    "EventCacheWriteCoordinator",
    "ThreadCacheState",
    "ThreadHistoryResult",
    "_EventCache",
    "_EventCacheWriteCoordinator",
    "normalize_nio_event_for_cache",
    "thread_cache_state_is_usable",
    "thread_history_result",
]
