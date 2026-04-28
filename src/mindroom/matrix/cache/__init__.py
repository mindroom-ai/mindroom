"""Matrix cache domain ownership.

Developer note:
- `event_cache.py` owns the storage-agnostic durable cache protocol.
- `event_normalization.py` owns storage-agnostic event payload shaping before backend writes.
- `sqlite_event_cache.py` owns the SQLite implementation, runtime, locking, and schema lifecycle.
- `postgres_event_cache.py` owns the PostgreSQL implementation, runtime, advisory locking, and schema lifecycle.
- `sqlite_event_cache_events.py` owns SQLite lookup/index rows, edits, and redaction tombstones.
- `sqlite_event_cache_threads.py` owns thread snapshot rows, cache-state reads, and thread/room invalidation state.
- `sqlite_agent_message_snapshot.py` owns SQLite reads for latest cached agent message snapshots.
- `postgres_event_cache_events.py`, `postgres_event_cache_threads.py`, and `postgres_agent_message_snapshot.py` own the equivalent PostgreSQL row helpers.
- `thread_writes.py` owns live, outbound, and sync mutation flows; `thread_bookkeeping.py` resolves thread impact and `thread_write_cache_ops.py` applies queued cache mutations.

Package boundary:
- `mindroom.matrix.cache` is the package-level import surface for cache-facing contracts and shared helpers used above the cache package.
- `SqliteEventCache`, `PostgresEventCache`, and `_EventCacheWriteCoordinator` remain private concrete services used by `runtime_support.py` through their concrete owner modules.
- `MatrixConversationCache` remains the higher-level conversation read/write facade above the cache package and may use specific cache helper submodules through narrow Tach visibility.

Main invariants:
- Runtime disable and room/db ordering live only in the concrete event-cache implementation.
- Event lookup rows and thread snapshot rows are written together so lookup, edit, and thread indexes stay consistent.
- Thread invalidation is durable state first, with fail-closed deletion only when stale markers cannot be written.
"""

from .agent_message_snapshot import (
    AgentMessageSnapshot,
    AgentMessageSnapshotUnavailable,
)
from .event_cache import ConversationEventCache, EventCacheBackendUnavailableError, ThreadCacheState
from .event_normalization import normalize_event_source_for_cache, normalize_nio_event_for_cache
from .thread_cache_helpers import thread_cache_rejection_reason, thread_cache_state_is_usable
from .thread_history_result import (
    THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC,
    THREAD_HISTORY_DEGRADED_DIAGNOSTIC,
    THREAD_HISTORY_ERROR_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_CACHE,
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_HOMESERVER,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
    ThreadHistoryResult,
    thread_history_result,
)
from .write_coordinator import EventCacheWriteCoordinator

__all__ = [
    "THREAD_HISTORY_CACHE_REJECT_REASON_DIAGNOSTIC",
    "THREAD_HISTORY_DEGRADED_DIAGNOSTIC",
    "THREAD_HISTORY_ERROR_DIAGNOSTIC",
    "THREAD_HISTORY_SOURCE_CACHE",
    "THREAD_HISTORY_SOURCE_DIAGNOSTIC",
    "THREAD_HISTORY_SOURCE_HOMESERVER",
    "THREAD_HISTORY_SOURCE_STALE_CACHE",
    "AgentMessageSnapshot",
    "AgentMessageSnapshotUnavailable",
    "ConversationEventCache",
    "EventCacheBackendUnavailableError",
    "EventCacheWriteCoordinator",
    "ThreadCacheState",
    "ThreadHistoryResult",
    "normalize_event_source_for_cache",
    "normalize_nio_event_for_cache",
    "thread_cache_rejection_reason",
    "thread_cache_state_is_usable",
    "thread_history_result",
]
