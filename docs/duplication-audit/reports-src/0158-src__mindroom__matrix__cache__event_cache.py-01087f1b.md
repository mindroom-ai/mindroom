Summary: top duplication candidates for this primary file:

- `ConversationEventCache` is a storage-agnostic protocol, but its method surface is implemented with near-identical wrapper/delegation code in `src/mindroom/matrix/cache/sqlite_event_cache.py` and `src/mindroom/matrix/cache/postgres_event_cache.py`.
- Thread freshness state behavior is duplicated between SQLite and PostgreSQL thread helper modules, including `ThreadCacheState` construction, stale checks, replace-if-not-newer policy, and incremental revalidation policy.
- `ThreadCacheState` has a structural mirror in `ThreadCacheStateLike`; this is related interface duplication, not independent behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ThreadCacheState	class	lines 13-20	related-only	ThreadCacheState fields ThreadCacheStateLike load_thread_cache_state	src/mindroom/matrix/cache/thread_cache_helpers.py:13; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:119; src/mindroom/matrix/cache/postgres_event_cache_threads.py:146
EventCacheBackendUnavailableError	class	lines 23-24	related-only	EventCacheBackendUnavailableError cache_backend_unavailable transient unavailable	src/mindroom/matrix/cache/postgres_event_cache.py:91; src/mindroom/runtime_support.py:159; src/mindroom/matrix/cache/thread_write_cache_ops.py:317
ConversationEventCache	class	lines 27-160	duplicate-found	ConversationEventCache implementation SqliteEventCache PostgresEventCache protocol methods	src/mindroom/matrix/cache/sqlite_event_cache.py:393; src/mindroom/matrix/cache/postgres_event_cache.py:662
ConversationEventCache.durable_writes_available	method	lines 31-32	duplicate-found	durable_writes_available property cache writes durable	src/mindroom/matrix/cache/sqlite_event_cache.py:410; src/mindroom/matrix/cache/postgres_event_cache.py:683
ConversationEventCache.is_initialized	method	lines 35-36	duplicate-found	is_initialized property cache runtime initialized	src/mindroom/matrix/cache/sqlite_event_cache.py:405; src/mindroom/matrix/cache/postgres_event_cache.py:679
ConversationEventCache.initialize	async_method	lines 38-39	duplicate-found	initialize event cache runtime initialize	src/mindroom/matrix/cache/sqlite_event_cache.py:430; src/mindroom/matrix/cache/postgres_event_cache.py:688
ConversationEventCache.close	async_method	lines 41-42	duplicate-found	close event cache runtime close	src/mindroom/matrix/cache/sqlite_event_cache.py:438; src/mindroom/matrix/cache/postgres_event_cache.py:717
ConversationEventCache.get_thread_events	async_method	lines 44-45	duplicate-found	get_thread_events load_thread_events delegation	src/mindroom/matrix/cache/sqlite_event_cache.py:474; src/mindroom/matrix/cache/postgres_event_cache.py:840; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:36; src/mindroom/matrix/cache/postgres_event_cache_threads.py:61
ConversationEventCache.get_recent_room_thread_ids	async_method	lines 47-48	duplicate-found	get_recent_room_thread_ids load_recent_room_thread_ids delegation	src/mindroom/matrix/cache/sqlite_event_cache.py:487; src/mindroom/matrix/cache/postgres_event_cache.py:854; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:59; src/mindroom/matrix/cache/postgres_event_cache_threads.py:84
ConversationEventCache.get_thread_cache_state	async_method	lines 50-51	duplicate-found	get_thread_cache_state load_thread_cache_state ThreadCacheState	src/mindroom/matrix/cache/sqlite_event_cache.py:500; src/mindroom/matrix/cache/postgres_event_cache.py:868; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:119; src/mindroom/matrix/cache/postgres_event_cache_threads.py:146
ConversationEventCache.get_event	async_method	lines 53-54	duplicate-found	get_event load_event disabled_result delegation	src/mindroom/matrix/cache/sqlite_event_cache.py:513; src/mindroom/matrix/cache/postgres_event_cache.py:882
ConversationEventCache.get_recent_room_events	async_method	lines 56-64	duplicate-found	get_recent_room_events load_recent_room_events delegation	src/mindroom/matrix/cache/sqlite_event_cache.py:522; src/mindroom/matrix/cache/postgres_event_cache.py:895
ConversationEventCache.get_latest_edit	async_method	lines 66-73	duplicate-found	get_latest_edit load_latest_edit sender original_event_id	src/mindroom/matrix/cache/sqlite_event_cache.py:544; src/mindroom/matrix/cache/postgres_event_cache.py:918
ConversationEventCache.get_latest_agent_message_snapshot	async_method	lines 75-83	duplicate-found	get_latest_agent_message_snapshot load agent snapshot sqlite postgres	src/mindroom/matrix/cache/sqlite_event_cache.py:564; src/mindroom/matrix/cache/postgres_event_cache.py:939; src/mindroom/hooks/context.py:343
ConversationEventCache.get_mxc_text	async_method	lines 85-86	duplicate-found	get_mxc_text load_mxc_text delegation	src/mindroom/matrix/cache/sqlite_event_cache.py:586; src/mindroom/matrix/cache/postgres_event_cache.py:962; src/mindroom/matrix/message_content.py:120
ConversationEventCache.store_event	async_method	lines 88-89	duplicate-found	store_event store_events_batch singleton	src/mindroom/matrix/cache/sqlite_event_cache.py:598; src/mindroom/matrix/cache/postgres_event_cache.py:975
ConversationEventCache.store_events_batch	async_method	lines 91-92	duplicate-found	store_events_batch normalize_event_source_for_cache events_by_room persist_lookup_events	src/mindroom/matrix/cache/sqlite_event_cache.py:602; src/mindroom/matrix/cache/postgres_event_cache.py:979
ConversationEventCache.store_mxc_text	async_method	lines 94-95	duplicate-found	store_mxc_text persist_mxc_text cached_at	src/mindroom/matrix/cache/sqlite_event_cache.py:628; src/mindroom/matrix/cache/postgres_event_cache.py:1006
ConversationEventCache.replace_thread	async_method	lines 97-105	duplicate-found	replace_thread replace_thread_locked validated_at time	src/mindroom/matrix/cache/sqlite_event_cache.py:642; src/mindroom/matrix/cache/postgres_event_cache.py:1021
ConversationEventCache.replace_thread_if_not_newer	async_method	lines 107-116	duplicate-found	replace_thread_if_not_newer replacement_validated_at fetch_started_at	src/mindroom/matrix/cache/sqlite_event_cache.py:664; src/mindroom/matrix/cache/postgres_event_cache.py:1044; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:275; src/mindroom/matrix/cache/postgres_event_cache_threads.py:299
ConversationEventCache.invalidate_thread	async_method	lines 118-119	duplicate-found	invalidate_thread invalidate_thread_locked delegation	src/mindroom/matrix/cache/sqlite_event_cache.py:695; src/mindroom/matrix/cache/postgres_event_cache.py:1076
ConversationEventCache.invalidate_room_threads	async_method	lines 121-122	duplicate-found	invalidate_room_threads invalidate_room_threads_locked delegation	src/mindroom/matrix/cache/sqlite_event_cache.py:708; src/mindroom/matrix/cache/postgres_event_cache.py:1090
ConversationEventCache.mark_thread_stale	async_method	lines 124-125	duplicate-found	mark_thread_stale mark_thread_stale_locked pending invalidation	src/mindroom/matrix/cache/sqlite_event_cache.py:720; src/mindroom/matrix/cache/postgres_event_cache.py:1103; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:382; src/mindroom/matrix/cache/postgres_event_cache_threads.py:420
ConversationEventCache.mark_room_threads_stale	async_method	lines 127-128	duplicate-found	mark_room_threads_stale mark_room_stale_locked pending invalidation	src/mindroom/matrix/cache/sqlite_event_cache.py:734; src/mindroom/matrix/cache/postgres_event_cache.py:1129; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:452; src/mindroom/matrix/cache/postgres_event_cache_threads.py:496
ConversationEventCache.append_event	async_method	lines 130-131	duplicate-found	append_event normalize_event_source_for_cache append_existing_thread_event	src/mindroom/matrix/cache/sqlite_event_cache.py:747; src/mindroom/matrix/cache/postgres_event_cache.py:1153
ConversationEventCache.revalidate_thread_after_incremental_update	async_method	lines 133-138	duplicate-found	revalidate_thread_after_incremental_update revalidation reasons validated invalidated room_invalidated	src/mindroom/matrix/cache/sqlite_event_cache.py:764; src/mindroom/matrix/cache/postgres_event_cache.py:1171; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:418; src/mindroom/matrix/cache/postgres_event_cache_threads.py:460
ConversationEventCache.get_thread_id_for_event	async_method	lines 140-141	duplicate-found	get_thread_id_for_event load_thread_id_for_event delegation	src/mindroom/matrix/cache/sqlite_event_cache.py:783; src/mindroom/matrix/cache/postgres_event_cache.py:1191
ConversationEventCache.redact_event	async_method	lines 143-148	duplicate-found	redact_event redact_event_locked disabled_result false	src/mindroom/matrix/cache/sqlite_event_cache.py:796; src/mindroom/matrix/cache/postgres_event_cache.py:1205; src/mindroom/matrix/cache/thread_write_cache_ops.py:151
ConversationEventCache.disable	method	lines 150-151	duplicate-found	disable advisory cache runtime disable	src/mindroom/matrix/cache/sqlite_event_cache.py:434; src/mindroom/matrix/cache/postgres_event_cache.py:713
ConversationEventCache.runtime_diagnostics	method	lines 153-154	duplicate-found	runtime_diagnostics cache_backend initialized disabled	src/mindroom/matrix/cache/sqlite_event_cache.py:414; src/mindroom/matrix/cache/postgres_event_cache.py:692; src/mindroom/matrix/cache/postgres_event_cache.py:420
ConversationEventCache.pending_durable_write_room_ids	method	lines 156-157	related-only	pending_durable_write_room_ids pending invalidation room ids	src/mindroom/matrix/cache/sqlite_event_cache.py:422; src/mindroom/matrix/cache/postgres_event_cache.py:696; src/mindroom/matrix/cache/thread_write_cache_ops.py:51
ConversationEventCache.flush_pending_durable_writes	async_method	lines 159-160	related-only	flush_pending_durable_writes pending invalidations durable writes	src/mindroom/matrix/cache/sqlite_event_cache.py:426; src/mindroom/matrix/cache/postgres_event_cache.py:700; src/mindroom/matrix/cache/thread_write_cache_ops.py:66
```

Findings:

1. `ConversationEventCache` facade methods are duplicated across SQLite and PostgreSQL backends.
   `SqliteEventCache` implements the protocol with `_read_operation`/`_write_operation` wrappers that delegate each method to a storage helper at `src/mindroom/matrix/cache/sqlite_event_cache.py:474`, `src/mindroom/matrix/cache/sqlite_event_cache.py:487`, `src/mindroom/matrix/cache/sqlite_event_cache.py:500`, `src/mindroom/matrix/cache/sqlite_event_cache.py:513`, and continuing through `src/mindroom/matrix/cache/sqlite_event_cache.py:796`.
   `PostgresEventCache` repeats the same facade shape at `src/mindroom/matrix/cache/postgres_event_cache.py:840`, `src/mindroom/matrix/cache/postgres_event_cache.py:854`, `src/mindroom/matrix/cache/postgres_event_cache.py:868`, `src/mindroom/matrix/cache/postgres_event_cache.py:882`, and continuing through `src/mindroom/matrix/cache/postgres_event_cache.py:1205`.
   The duplicated behavior is the API-level mapping of each protocol method to a read/write operation, disabled fallback, event normalization, timestamp defaulting, and bool coercion.
   Differences to preserve: PostgreSQL carries a namespace parameter, transient backend failure handling, pending invalidation flushing, and pending durable write semantics; SQLite has direct single-connection semantics and no pending writes.

2. Batch event storage logic is nearly identical in the two facade classes.
   `SqliteEventCache.store_events_batch` groups events by room, normalizes with `normalize_event_source_for_cache`, captures `cached_at`, and calls `persist_lookup_events` at `src/mindroom/matrix/cache/sqlite_event_cache.py:602`.
   `PostgresEventCache.store_events_batch` repeats the same grouping and normalization flow at `src/mindroom/matrix/cache/postgres_event_cache.py:979`.
   Differences to preserve: PostgreSQL passes `namespace` to its storage helper and runs through the PostgreSQL operation wrapper.

3. Thread state construction and freshness policy are duplicated in backend-specific thread helper modules.
   SQLite constructs `ThreadCacheState` from a joined thread/room state row at `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:119`.
   PostgreSQL constructs the same object from equivalent row positions at `src/mindroom/matrix/cache/postgres_event_cache_threads.py:146`.
   SQLite and PostgreSQL also duplicate `_thread_cache_state_changed_after` at `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:260` and `src/mindroom/matrix/cache/postgres_event_cache_threads.py:284`, plus incremental revalidation policy at `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:418` and `src/mindroom/matrix/cache/postgres_event_cache_threads.py:460`.
   Differences to preserve: table names, parameter style, namespace, and PostgreSQL's optional externally supplied invalidation timestamp.

4. `ThreadCacheState` has a related structural protocol mirror.
   `ThreadCacheState` declares the durable state fields at `src/mindroom/matrix/cache/event_cache.py:13`.
   `ThreadCacheStateLike` repeats the same five attributes at `src/mindroom/matrix/cache/thread_cache_helpers.py:13` so helper functions can be structural.
   This is related-only because it intentionally avoids depending on the concrete dataclass, but changes to state field names or meanings must be updated in both places.

Proposed generalization:

- No refactor recommended for `src/mindroom/matrix/cache/event_cache.py` itself; it is the canonical protocol and dataclass contract.
- If this duplication becomes active maintenance pain, extract backend-neutral pure helpers near `src/mindroom/matrix/cache/thread_cache_helpers.py` for row-to-`ThreadCacheState`, `_thread_cache_state_changed_after`, replacement validation timestamp selection, and incremental revalidation eligibility.
- A broader facade abstraction for SQLite/PostgreSQL method delegation is possible, but not recommended as a first step because PostgreSQL's retry, namespace, pending invalidation, and durable-write behavior would make a generic base class easy to overfit.

Risk/tests:

- Main behavior risk is cache freshness regression: `replace_thread_if_not_newer`, stale markers, and incremental revalidation must keep exactly the same timestamp comparisons.
- Tests should cover both SQLite and PostgreSQL helpers for `ThreadCacheState` row conversion, stale-after-fetch rejection, room invalidation interaction, and safe incremental revalidation.
- Facade-level tests should verify disabled fallbacks, normalization before persistence, default timestamp choices, and PostgreSQL pending invalidation flushing.
