"""Matrix cache domain ownership.

Developer note:
- `event_cache.py` owns the storage-agnostic durable cache protocol.
- `event_normalization.py` owns storage-agnostic event payload shaping before backend writes.
- `event_cache_events.py` owns backend-neutral serialized event values, indexes, and redaction decisions.
- `cache_maintenance.py` owns backend-neutral maintenance reports and streaming-status categories.
- `thread_cache_state.py` owns backend-neutral durable state values and comparison rules.
- `agent_message_snapshot_semantics.py` owns backend-neutral latest-message selection rules.
- `sqlite_event_cache.py` owns the SQLite implementation, runtime, locking, and schema lifecycle.
- `postgres_event_cache.py` owns the PostgreSQL implementation, runtime, advisory locking, and schema lifecycle.
- `sqlite_event_cache_events.py` owns SQLite lookup/index rows, edits, and redaction tombstones.
- `sqlite_event_cache_threads.py` owns thread snapshot rows, cache-state reads, and thread/room invalidation state.
- `sqlite_agent_message_snapshot.py` owns SQLite reads for latest cached agent message snapshots.
- `postgres_event_cache_events.py`, `postgres_event_cache_threads.py`, and `postgres_agent_message_snapshot.py` own the equivalent PostgreSQL row helpers.
- `sqlite_cache_maintenance.py` and `postgres_cache_maintenance.py` own transactional migration, invariant repair, and startup diagnostics.
- `sqlite_streaming_compaction.py` and `postgres_streaming_compaction.py` own compressed cold history for superseded streaming edits.
- `thread_writes.py` owns live, outbound, and sync mutation flows; `thread_bookkeeping.py` resolves thread impact and `thread_write_cache_ops.py` applies queued cache mutations.

Package boundary:
- `mindroom.matrix.cache` is the package-level import surface for cache-facing contracts and shared helpers used above the cache package.
- `SqliteEventCache`, `PostgresEventCache`, and `EventCacheWriteCoordinator` remain private concrete services used by `runtime_support.py` through their concrete owner modules.
- `MatrixConversationCache` remains the higher-level conversation read/write facade above the cache package and may use specific cache helper submodules through narrow Tach visibility.

Main invariants:
- Runtime disable and room/db ordering live only in the concrete event-cache implementation.
- Event lookup rows and thread snapshot rows are written together so lookup, edit, and thread indexes stay consistent.
- Full event JSON has one active source of truth, while compacted nonterminal streaming edits retain one compressed redaction-safe cold copy.
- Thread invalidation is durable state first, with fail-closed deletion only when stale markers cannot be written.
"""

from .agent_message_snapshot import AgentMessageSnapshot
from .event_cache import ConversationEventCache, ThreadCacheState
from .event_normalization import normalize_nio_event_for_cache
from .thread_cache_helpers import thread_cache_rejection_reason
from .thread_history_result import ThreadHistoryResult, thread_history_result
from .write_coordinator import EventCacheWriteCoordinator

__all__ = [
    "AgentMessageSnapshot",
    "ConversationEventCache",
    "EventCacheWriteCoordinator",
    "ThreadCacheState",
    "ThreadHistoryResult",
    "normalize_nio_event_for_cache",
    "thread_cache_rejection_reason",
    "thread_history_result",
]
