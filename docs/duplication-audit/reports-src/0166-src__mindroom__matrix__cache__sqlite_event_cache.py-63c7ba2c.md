## Summary

The strongest duplication candidate is the SQLite event-cache facade in `src/mindroom/matrix/cache/sqlite_event_cache.py`, which mirrors the PostgreSQL facade in `src/mindroom/matrix/cache/postgres_event_cache.py` for nearly every public cache method.
The runtime lock bookkeeping and schema definitions are also duplicated conceptually across the SQLite and PostgreSQL backends, but backend-specific connection, namespace, retry, and invalidation semantics make a broad refactor risky.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_close_sqlite_connection_best_effort	async_function	lines 46-56	duplicate-found	best_effort close sqlite postgres event cache connection	src/mindroom/matrix/cache/postgres_event_cache.py:116
_rollback_sqlite_connection_best_effort	async_function	lines 59-69	duplicate-found	best_effort rollback sqlite postgres event cache connection	src/mindroom/matrix/cache/postgres_event_cache.py:97
initialize_event_cache_db	async_function	lines 72-85	related-only	initialize event cache db create schema commit close on failure	src/mindroom/matrix/cache/postgres_event_cache.py:135
create_event_cache_schema	async_function	lines 88-196	duplicate-found	event cache schema tables thread_events events edits mxc thread_state	src/mindroom/matrix/cache/postgres_event_cache.py:153
schema_version	async_function	lines 199-204	related-only	schema_version PRAGMA user_version metadata schema version	src/mindroom/matrix/cache/postgres_event_cache.py:292
existing_table_names	async_function	lines 207-218	none-found	sqlite_master existing table names event cache	none
reset_stale_cache_if_needed	async_function	lines 221-246	related-only	validate reset stale schema migration event cache	src/mindroom/matrix/cache/postgres_event_cache.py:308
_RoomLockEntry	class	lines 250-254	duplicate-found	RoomLockEntry active_users asyncio Lock	src/mindroom/matrix/cache/postgres_event_cache.py:334
_SqliteEventCacheRuntime	class	lines 257-386	duplicate-found	EventCacheRuntime db_lock room_locks disable acquire_db_operation prune	src/mindroom/matrix/cache/postgres_event_cache.py:350
_SqliteEventCacheRuntime.__init__	method	lines 260-265	duplicate-found	runtime init db lock room locks disabled reason	src/mindroom/matrix/cache/postgres_event_cache.py:353
_SqliteEventCacheRuntime.db_path	method	lines 268-270	not-a-behavior-symbol	property returns stored db path	none
_SqliteEventCacheRuntime.db	method	lines 273-275	not-a-behavior-symbol	property returns active db	none
_SqliteEventCacheRuntime.room_locks	method	lines 278-280	not-a-behavior-symbol	property returns room lock table	src/mindroom/matrix/cache/postgres_event_cache.py:388
_SqliteEventCacheRuntime.is_initialized	method	lines 283-285	related-only	is_initialized db is not None connection open	src/mindroom/matrix/cache/postgres_event_cache.py:393
_SqliteEventCacheRuntime.is_disabled	method	lines 288-290	not-a-behavior-symbol	property returns disabled reason presence	src/mindroom/matrix/cache/postgres_event_cache.py:398
_SqliteEventCacheRuntime.disable	method	lines 292-301	duplicate-found	disable advisory Matrix event cache disabled_reason logger warning	src/mindroom/matrix/cache/postgres_event_cache.py:408
_SqliteEventCacheRuntime.initialize	async_method	lines 303-308	related-only	runtime initialize db_lock disabled db is none initialize db	src/mindroom/matrix/cache/postgres_event_cache.py:432
_SqliteEventCacheRuntime.close	async_method	lines 310-317	related-only	runtime close db_lock close clear room_locks	src/mindroom/matrix/cache/postgres_event_cache.py:468
_SqliteEventCacheRuntime.room_lock_entry	method	lines 319-329	duplicate-found	room_lock_entry active_user_increment move_to_end prune	src/mindroom/matrix/cache/postgres_event_cache.py:487
_SqliteEventCacheRuntime.acquire_room_lock	async_method	lines 332-354	duplicate-found	acquire_room_lock wait_time lock threshold active_users prune	src/mindroom/matrix/cache/postgres_event_cache.py:500
_SqliteEventCacheRuntime.acquire_db_operation	async_method	lines 357-367	related-only	acquire_db_operation initialize db_lock room lock require db	src/mindroom/matrix/cache/postgres_event_cache.py:525
_SqliteEventCacheRuntime.require_db	method	lines 369-374	related-only	require_db raise uninitialized event cache	src/mindroom/matrix/cache/postgres_event_cache.py:553
_SqliteEventCacheRuntime._prune_room_locks	method	lines 376-386	duplicate-found	prune room locks max cached active_users OrderedDict	src/mindroom/matrix/cache/postgres_event_cache.py:649
SqliteEventCache	class	lines 393-813	duplicate-found	SqliteEventCache PostgresEventCache public facade methods	src/mindroom/matrix/cache/postgres_event_cache.py:662
SqliteEventCache.__init__	method	lines 396-397	related-only	facade init runtime backend	src/mindroom/matrix/cache/postgres_event_cache.py:665
SqliteEventCache.db_path	method	lines 400-402	not-a-behavior-symbol	property returns runtime db path	none
SqliteEventCache.is_initialized	method	lines 405-407	not-a-behavior-symbol	property delegates runtime is_initialized	src/mindroom/matrix/cache/postgres_event_cache.py:678
SqliteEventCache.durable_writes_available	method	lines 410-412	related-only	durable_writes_available initialized disabled runtime	src/mindroom/matrix/cache/postgres_event_cache.py:683
SqliteEventCache.runtime_diagnostics	method	lines 414-420	related-only	runtime diagnostics cache backend initialized disabled	src/mindroom/matrix/cache/postgres_event_cache.py:692
SqliteEventCache.pending_durable_write_room_ids	method	lines 422-424	related-only	pending durable write room ids event cache	src/mindroom/matrix/cache/postgres_event_cache.py:696
SqliteEventCache.flush_pending_durable_writes	async_method	lines 426-428	related-only	flush pending durable writes room id	src/mindroom/matrix/cache/postgres_event_cache.py:700
SqliteEventCache.initialize	async_method	lines 430-432	not-a-behavior-symbol	delegates runtime initialize	src/mindroom/matrix/cache/postgres_event_cache.py:688
SqliteEventCache.disable	method	lines 434-436	not-a-behavior-symbol	delegates runtime disable	src/mindroom/matrix/cache/postgres_event_cache.py:713
SqliteEventCache.close	async_method	lines 438-440	not-a-behavior-symbol	delegates runtime close	src/mindroom/matrix/cache/postgres_event_cache.py:717
SqliteEventCache._read_operation	async_method	lines 442-453	related-only	disabled_result acquire_db_operation callback read operation	src/mindroom/matrix/cache/postgres_event_cache.py:721
SqliteEventCache._write_operation	async_method	lines 455-472	related-only	disabled_result acquire_db_operation callback commit rollback	src/mindroom/matrix/cache/postgres_event_cache.py:736
SqliteEventCache.get_thread_events	async_method	lines 474-485	duplicate-found	get_thread_events read_operation load_thread_events disabled None	src/mindroom/matrix/cache/postgres_event_cache.py:840
SqliteEventCache.get_recent_room_thread_ids	async_method	lines 487-498	duplicate-found	get_recent_room_thread_ids load_recent_room_thread_ids disabled empty list	src/mindroom/matrix/cache/postgres_event_cache.py:854
SqliteEventCache.get_thread_cache_state	async_method	lines 500-511	duplicate-found	get_thread_cache_state load_thread_cache_state disabled None	src/mindroom/matrix/cache/postgres_event_cache.py:868
SqliteEventCache.get_event	async_method	lines 513-520	duplicate-found	get_event load_event disabled None	src/mindroom/matrix/cache/postgres_event_cache.py:882
SqliteEventCache.get_recent_room_events	async_method	lines 522-542	duplicate-found	get_recent_room_events load_recent_room_events event_type since limit	src/mindroom/matrix/cache/postgres_event_cache.py:895
SqliteEventCache.get_latest_edit	async_method	lines 544-562	duplicate-found	get_latest_edit load_latest_edit original_event_id sender	src/mindroom/matrix/cache/postgres_event_cache.py:918
SqliteEventCache.get_latest_agent_message_snapshot	async_method	lines 564-584	duplicate-found	get_latest_agent_message_snapshot latest visible cached sender scope	src/mindroom/matrix/cache/postgres_event_cache.py:939
SqliteEventCache.get_mxc_text	async_method	lines 586-596	duplicate-found	get_mxc_text load_mxc_text disabled None	src/mindroom/matrix/cache/postgres_event_cache.py:962
SqliteEventCache.store_event	async_method	lines 598-600	duplicate-found	store_event wraps store_events_batch single tuple	src/mindroom/matrix/cache/postgres_event_cache.py:975
SqliteEventCache.store_events_batch	async_method	lines 602-626	duplicate-found	store_events_batch normalize_event_source_for_cache events_by_room cached_at	src/mindroom/matrix/cache/postgres_event_cache.py:979
SqliteEventCache.store_mxc_text	async_method	lines 628-640	duplicate-found	store_mxc_text persist_mxc_text cached_at time	src/mindroom/matrix/cache/postgres_event_cache.py:1006
SqliteEventCache.replace_thread	async_method	lines 642-662	duplicate-found	replace_thread replace_thread_locked validated_at default time	src/mindroom/matrix/cache/postgres_event_cache.py:1021
SqliteEventCache.replace_thread_if_not_newer	async_method	lines 664-693	duplicate-found	replace_thread_if_not_newer fetch_started_at min validated_at	src/mindroom/matrix/cache/postgres_event_cache.py:1044
SqliteEventCache.replace_thread_if_not_newer.<locals>.replace_if_still_safe	nested_async_function	lines 676-684	duplicate-found	replace_if_still_safe replace_thread_locked_if_not_newer	src/mindroom/matrix/cache/postgres_event_cache.py:1056
SqliteEventCache.invalidate_thread	async_method	lines 695-706	duplicate-found	invalidate_thread invalidate_thread_locked disabled None	src/mindroom/matrix/cache/postgres_event_cache.py:1076
SqliteEventCache.invalidate_room_threads	async_method	lines 708-718	duplicate-found	invalidate_room_threads invalidate_room_threads_locked disabled None	src/mindroom/matrix/cache/postgres_event_cache.py:1090
SqliteEventCache.mark_thread_stale	async_method	lines 720-732	related-only	mark_thread_stale mark_thread_stale_locked reason invalidated_at	src/mindroom/matrix/cache/postgres_event_cache.py:1103
SqliteEventCache.mark_room_threads_stale	async_method	lines 734-745	related-only	mark_room_threads_stale mark_room_stale_locked reason invalidated_at	src/mindroom/matrix/cache/postgres_event_cache.py:1129
SqliteEventCache.append_event	async_method	lines 747-762	duplicate-found	append_event normalize_event_source_for_cache append_existing_thread_event	src/mindroom/matrix/cache/postgres_event_cache.py:1153
SqliteEventCache.revalidate_thread_after_incremental_update	async_method	lines 764-781	duplicate-found	revalidate_thread_after_incremental_update revalidate locked disabled false	src/mindroom/matrix/cache/postgres_event_cache.py:1171
SqliteEventCache.get_thread_id_for_event	async_method	lines 783-794	duplicate-found	get_thread_id_for_event load_thread_id_for_event disabled None	src/mindroom/matrix/cache/postgres_event_cache.py:1191
SqliteEventCache.redact_event	async_method	lines 796-813	duplicate-found	redact_event redact_event_locked disabled false	src/mindroom/matrix/cache/postgres_event_cache.py:1205
```

## Findings

1. `SqliteEventCache` repeats the public cache facade implemented by `PostgresEventCache`.
   The SQLite methods at `src/mindroom/matrix/cache/sqlite_event_cache.py:474`, `src/mindroom/matrix/cache/sqlite_event_cache.py:487`, `src/mindroom/matrix/cache/sqlite_event_cache.py:500`, `src/mindroom/matrix/cache/sqlite_event_cache.py:513`, `src/mindroom/matrix/cache/sqlite_event_cache.py:522`, `src/mindroom/matrix/cache/sqlite_event_cache.py:544`, `src/mindroom/matrix/cache/sqlite_event_cache.py:564`, `src/mindroom/matrix/cache/sqlite_event_cache.py:586`, `src/mindroom/matrix/cache/sqlite_event_cache.py:598`, `src/mindroom/matrix/cache/sqlite_event_cache.py:602`, `src/mindroom/matrix/cache/sqlite_event_cache.py:628`, `src/mindroom/matrix/cache/sqlite_event_cache.py:642`, `src/mindroom/matrix/cache/sqlite_event_cache.py:664`, `src/mindroom/matrix/cache/sqlite_event_cache.py:695`, `src/mindroom/matrix/cache/sqlite_event_cache.py:708`, `src/mindroom/matrix/cache/sqlite_event_cache.py:747`, `src/mindroom/matrix/cache/sqlite_event_cache.py:764`, `src/mindroom/matrix/cache/sqlite_event_cache.py:783`, and `src/mindroom/matrix/cache/sqlite_event_cache.py:796` have direct counterparts in `src/mindroom/matrix/cache/postgres_event_cache.py:840`, `src/mindroom/matrix/cache/postgres_event_cache.py:854`, `src/mindroom/matrix/cache/postgres_event_cache.py:868`, `src/mindroom/matrix/cache/postgres_event_cache.py:882`, `src/mindroom/matrix/cache/postgres_event_cache.py:895`, `src/mindroom/matrix/cache/postgres_event_cache.py:918`, `src/mindroom/matrix/cache/postgres_event_cache.py:939`, `src/mindroom/matrix/cache/postgres_event_cache.py:962`, `src/mindroom/matrix/cache/postgres_event_cache.py:975`, `src/mindroom/matrix/cache/postgres_event_cache.py:979`, `src/mindroom/matrix/cache/postgres_event_cache.py:1006`, `src/mindroom/matrix/cache/postgres_event_cache.py:1021`, `src/mindroom/matrix/cache/postgres_event_cache.py:1044`, `src/mindroom/matrix/cache/postgres_event_cache.py:1076`, `src/mindroom/matrix/cache/postgres_event_cache.py:1090`, `src/mindroom/matrix/cache/postgres_event_cache.py:1153`, `src/mindroom/matrix/cache/postgres_event_cache.py:1171`, `src/mindroom/matrix/cache/postgres_event_cache.py:1191`, and `src/mindroom/matrix/cache/postgres_event_cache.py:1205`.
   The behavior is the same facade pattern: check disabled state through `_read_operation` or `_write_operation`, delegate to backend-specific event/thread helpers, normalize event payloads before storing, and coerce optional write results to booleans.
   The main differences to preserve are PostgreSQL namespace arguments, PostgreSQL transient retry and pending-invalidation flushing, and backend-specific helper modules.

2. Room lock runtime bookkeeping is duplicated between the SQLite and PostgreSQL runtimes.
   `_RoomLockEntry`, `room_lock_entry`, `acquire_room_lock`, and `_prune_room_locks` in `src/mindroom/matrix/cache/sqlite_event_cache.py:249`, `src/mindroom/matrix/cache/sqlite_event_cache.py:319`, `src/mindroom/matrix/cache/sqlite_event_cache.py:331`, and `src/mindroom/matrix/cache/sqlite_event_cache.py:376` match the PostgreSQL implementations at `src/mindroom/matrix/cache/postgres_event_cache.py:334`, `src/mindroom/matrix/cache/postgres_event_cache.py:487`, `src/mindroom/matrix/cache/postgres_event_cache.py:500`, and `src/mindroom/matrix/cache/postgres_event_cache.py:649`.
   Both maintain an `OrderedDict` of per-room locks, increment active users before waiting, log waits over the same threshold, release and decrement in `finally`, and evict inactive locks once the cache exceeds the maximum.
   The only functional difference is log message/backend naming.

3. Event-cache schema shape is duplicated across SQLite and PostgreSQL.
   SQLite creates `thread_events`, `events`, `event_edits`, `event_threads`, `redacted_events`, `mxc_text_cache`, `thread_cache_state`, and `room_cache_state` at `src/mindroom/matrix/cache/sqlite_event_cache.py:88`.
   PostgreSQL creates the same logical tables with prefixed names, a namespace column, write sequence fields, and PostgreSQL types at `src/mindroom/matrix/cache/postgres_event_cache.py:153`.
   This is real duplication of the data model, but the SQL dialect and PostgreSQL write-sequence/namespace requirements make a shared SQL string generator higher risk than the facade and locking opportunities.

4. Best-effort close and rollback wrappers are duplicated in backend-specific form.
   SQLite defines `_close_sqlite_connection_best_effort` and `_rollback_sqlite_connection_best_effort` at `src/mindroom/matrix/cache/sqlite_event_cache.py:46` and `src/mindroom/matrix/cache/sqlite_event_cache.py:59`; PostgreSQL defines matching wrappers at `src/mindroom/matrix/cache/postgres_event_cache.py:97` and `src/mindroom/matrix/cache/postgres_event_cache.py:116`.
   Both catch cleanup exceptions and log debug metadata without masking the original failure.
   The differences are backend labels and PostgreSQL namespace metadata.

## Proposed Generalization

Prefer a small shared room-lock helper first, for example `src/mindroom/matrix/cache/room_lock_registry.py` containing the lock-entry dataclass and acquire/prune logic with a backend label for logging.
This would remove exact runtime duplication without touching database semantics.

For the public facade duplication, a minimal next step would be a typed backend adapter protocol plus shared pure helpers for operations that are backend-neutral today: event-batch normalization/grouping, `replacement_validated_at` calculation, and single-event-to-batch wrapping.
Avoid moving the whole facade into a base class unless PostgreSQL pending invalidations and transient retry behavior can stay explicit and easy to test.

No schema refactor is recommended now.
The duplicated schema is intentional backend parity, and a schema generator would add abstraction around dialect differences, namespace columns, write sequences, and stale-schema handling.

## Risk/tests

The lock helper has low behavior risk but needs concurrency tests covering wait logging, active-user decrement on callback failure, and eviction skipping active locks for both backends.
Facade helper extraction has moderate risk because PostgreSQL write operations commit reads, flush pending invalidations, retry transient failures, and may record pending stale markers where SQLite does not.
Any facade refactor should run the SQLite and PostgreSQL event-cache test suites, with focused coverage for `store_events_batch`, `replace_thread_if_not_newer`, stale-marker writes, redaction, and disabled-cache return values.
