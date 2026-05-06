## Summary

Top duplication candidates are the PostgreSQL and SQLite event-cache runtime shells, the cache protocol forwarding methods, and the schema/table creation shape.
The duplication is real but partly intentional because each backend has different connection, SQL, transaction, and namespacing semantics.
No production refactor is recommended without first extracting very small pure helpers, because a broad backend abstraction would likely hide important database differences.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_postgres_error_sqlstate	function	lines 63-71	none-found	postgres sqlstate psycopg diag	sqlite_event_cache.py none; src/mindroom rg sqlstate only postgres_event_cache.py
_is_transient_postgres_failure	function	lines 74-88	none-found	transient postgres operational interface error	sqlite_event_cache.py none; runtime_support.py:159 related catch only
_cache_backend_unavailable	function	lines 91-94	related-only	EventCacheBackendUnavailableError unavailable during	runtime_support.py:159, sync_certification.py:166
_rollback_postgres_connection_best_effort	async_function	lines 97-113	duplicate-found	rollback best effort connection failure	sqlite_event_cache.py:59
_close_postgres_connection_best_effort	async_function	lines 116-132	duplicate-found	close best effort connection failure	sqlite_event_cache.py:46
initialize_postgres_event_cache_db	async_function	lines 135-150	duplicate-found	initialize event cache db create schema commit	sqlite_event_cache.py:72
create_postgres_event_cache_schema	async_function	lines 153-289	duplicate-found	create event cache schema tables	sqlite_event_cache.py:88
postgres_schema_version	async_function	lines 292-305	related-only	schema_version metadata user_version	sqlite_event_cache.py:190
validate_postgres_event_cache_schema	async_function	lines 308-331	related-only	validate schema version stale reset	sqlite_event_cache.py:207
_RoomLockEntry	class	lines 335-339	duplicate-found	RoomLockEntry active_users lock	sqlite_event_cache.py:249
_PendingInvalidation	class	lines 343-347	none-found	pending invalidation invalidated_at reason	none
_PostgresEventCacheRuntime	class	lines 350-655	duplicate-found	EventCacheRuntime lifecycle locking disable	sqlite_event_cache.py:255
_PostgresEventCacheRuntime.__init__	method	lines 353-366	duplicate-found	runtime init db lock room locks disabled	sqlite_event_cache.py:258
_PostgresEventCacheRuntime.database_url	method	lines 369-371	not-a-behavior-symbol	property passthrough database_url	none
_PostgresEventCacheRuntime.redacted_database_url	method	lines 374-376	related-only	redacted database url postgres connection	runtime_support.py:173
_PostgresEventCacheRuntime.namespace	method	lines 379-381	not-a-behavior-symbol	property passthrough namespace	none
_PostgresEventCacheRuntime.db	method	lines 384-386	not-a-behavior-symbol	property passthrough db	sqlite_event_cache.py:270
_PostgresEventCacheRuntime.room_locks	method	lines 389-391	not-a-behavior-symbol	property passthrough room_locks	sqlite_event_cache.py:275
_PostgresEventCacheRuntime.is_initialized	method	lines 394-396	duplicate-found	is_initialized connection open	sqlite_event_cache.py:280
_PostgresEventCacheRuntime.is_disabled	method	lines 399-401	duplicate-found	is_disabled disabled_reason	sqlite_event_cache.py:285
_PostgresEventCacheRuntime.durable_writes_available	method	lines 404-406	related-only	durable_writes_available disabled closed	sqlite_event_cache.py:410
_PostgresEventCacheRuntime.disable	method	lines 408-418	duplicate-found	disable advisory Matrix event cache	sqlite_event_cache.py:290
_PostgresEventCacheRuntime.runtime_diagnostics	method	lines 420-436	duplicate-found	runtime diagnostics cache backend initialized disabled	sqlite_event_cache.py:414
_PostgresEventCacheRuntime.initialize	async_method	lines 438-460	duplicate-found	runtime initialize db lock schema	sqlite_event_cache.py:300
_PostgresEventCacheRuntime.close	async_method	lines 462-468	duplicate-found	runtime close db clear room locks	sqlite_event_cache.py:306
_PostgresEventCacheRuntime.handle_transient_failure	async_method	lines 470-485	none-found	transient failure reconnect close unavailable	none
_PostgresEventCacheRuntime.record_pending_thread_invalidation	method	lines 487-503	none-found	pending thread invalidation record	none
_PostgresEventCacheRuntime.record_pending_room_invalidation	method	lines 505-519	none-found	pending room invalidation record	none
_PostgresEventCacheRuntime.pending_room_invalidation	method	lines 521-523	not-a-behavior-symbol	pending room invalidation getter	none
_PostgresEventCacheRuntime.pending_thread_invalidations	method	lines 525-531	none-found	pending thread invalidations for room	none
_PostgresEventCacheRuntime.pending_invalidation_room_ids	method	lines 533-537	none-found	pending invalidation room ids	none
_PostgresEventCacheRuntime.forget_pending_room_invalidation	method	lines 539-547	none-found	forget pending room invalidation	none
_PostgresEventCacheRuntime.forget_pending_thread_invalidation	method	lines 549-558	none-found	forget pending thread invalidation	none
_PostgresEventCacheRuntime._close_db_locked	async_method	lines 560-566	related-only	close db locked best effort	sqlite_event_cache.py:306
_PostgresEventCacheRuntime.connection_is_closed	method	lines 568-570	none-found	connection is closed psycopg closed	none
_PostgresEventCacheRuntime._unavailable_reason_from_exception	method	lines 572-576	none-found	unavailable reason exception truncate	none
_PostgresEventCacheRuntime.room_lock_entry	method	lines 578-588	duplicate-found	room_lock_entry active_user_increment prune	sqlite_event_cache.py:313
_PostgresEventCacheRuntime.acquire_room_lock	async_method	lines 591-614	duplicate-found	acquire room lock wait log prune	sqlite_event_cache.py:327
_PostgresEventCacheRuntime.acquire_db_operation	async_method	lines 617-636	duplicate-found	acquire db operation db lock room lock	sqlite_event_cache.py:358
_PostgresEventCacheRuntime.require_db	method	lines 638-643	duplicate-found	require db uninitialized runtime error	sqlite_event_cache.py:370
_PostgresEventCacheRuntime._prune_room_locks	method	lines 645-655	duplicate-found	prune room locks active users max cached	sqlite_event_cache.py:376
PostgresEventCache	class	lines 662-1223	duplicate-found	ConversationEventCache implementation methods	sqlite_event_cache.py:393
PostgresEventCache.__init__	method	lines 665-666	duplicate-found	init runtime event cache	sqlite_event_cache.py:396
PostgresEventCache.database_url	method	lines 669-671	not-a-behavior-symbol	property passthrough database_url	none
PostgresEventCache.namespace	method	lines 674-676	not-a-behavior-symbol	property passthrough namespace	none
PostgresEventCache.is_initialized	method	lines 679-681	duplicate-found	is_initialized runtime property	sqlite_event_cache.py:405
PostgresEventCache.durable_writes_available	method	lines 684-686	duplicate-found	durable_writes_available runtime property	sqlite_event_cache.py:410
PostgresEventCache.initialize	async_method	lines 688-690	duplicate-found	initialize runtime forwarding	sqlite_event_cache.py:438
PostgresEventCache.runtime_diagnostics	method	lines 692-694	duplicate-found	runtime_diagnostics forwarding/backend dict	sqlite_event_cache.py:414
PostgresEventCache.pending_durable_write_room_ids	method	lines 696-698	related-only	pending durable write room ids	sqlite_event_cache.py:421
PostgresEventCache.flush_pending_durable_writes	async_method	lines 700-711	related-only	flush pending durable writes	sqlite_event_cache.py:425
PostgresEventCache.flush_pending_durable_writes.<locals>.flush_only	nested_async_function	lines 703-704	not-a-behavior-symbol	no-op flush callback	sqlite_event_cache.py:425
PostgresEventCache.disable	method	lines 713-715	duplicate-found	disable runtime forwarding	sqlite_event_cache.py:441
PostgresEventCache.close	async_method	lines 717-719	duplicate-found	close runtime forwarding	sqlite_event_cache.py:445
PostgresEventCache._read_operation	async_method	lines 721-734	duplicate-found	read operation disabled result acquire db	sqlite_event_cache.py:449
PostgresEventCache._write_operation	async_method	lines 736-749	duplicate-found	write operation disabled result commit rollback	sqlite_event_cache.py:462
PostgresEventCache._operation	async_method	lines 751-794	related-only	operation wrapper commit rollback retry	sqlite_event_cache.py:449, sqlite_event_cache.py:462
PostgresEventCache._flush_pending_invalidations	async_method	lines 796-826	none-found	flush pending invalidations mark stale	none
PostgresEventCache._forget_flushed_pending_invalidations	method	lines 828-838	none-found	forget flushed pending invalidations	none
PostgresEventCache.get_thread_events	async_method	lines 840-852	duplicate-found	get_thread_events delegate	sqlite_event_cache.py:480
PostgresEventCache.get_recent_room_thread_ids	async_method	lines 854-866	duplicate-found	get_recent_room_thread_ids delegate	sqlite_event_cache.py:493
PostgresEventCache.get_thread_cache_state	async_method	lines 868-880	duplicate-found	get_thread_cache_state delegate	sqlite_event_cache.py:507
PostgresEventCache.get_event	async_method	lines 882-893	duplicate-found	get_event delegate	sqlite_event_cache.py:520
PostgresEventCache.get_recent_room_events	async_method	lines 895-916	duplicate-found	get_recent_room_events delegate	sqlite_event_cache.py:530
PostgresEventCache.get_latest_edit	async_method	lines 918-937	duplicate-found	get_latest_edit delegate	sqlite_event_cache.py:544
PostgresEventCache.get_latest_agent_message_snapshot	async_method	lines 939-960	duplicate-found	get_latest_agent_message_snapshot delegate	sqlite_event_cache.py:562
PostgresEventCache.get_mxc_text	async_method	lines 962-973	duplicate-found	get_mxc_text delegate	sqlite_event_cache.py:586
PostgresEventCache.store_event	async_method	lines 975-977	duplicate-found	store_event store_events_batch wrapper	sqlite_event_cache.py:598
PostgresEventCache.store_events_batch	async_method	lines 979-1004	duplicate-found	store_events_batch normalize group by room	sqlite_event_cache.py:602
PostgresEventCache.store_mxc_text	async_method	lines 1006-1019	duplicate-found	store_mxc_text delegate cached_at	sqlite_event_cache.py:628
PostgresEventCache.replace_thread	async_method	lines 1021-1042	duplicate-found	replace_thread delegate validated_at	sqlite_event_cache.py:642
PostgresEventCache.replace_thread_if_not_newer	async_method	lines 1044-1074	duplicate-found	replace_thread_if_not_newer validated min nested callback	sqlite_event_cache.py:664
PostgresEventCache.replace_thread_if_not_newer.<locals>.replace_if_still_safe	nested_async_function	lines 1056-1065	duplicate-found	replace_if_still_safe nested callback	sqlite_event_cache.py:676
PostgresEventCache.invalidate_thread	async_method	lines 1076-1088	duplicate-found	invalidate_thread delegate	sqlite_event_cache.py:695
PostgresEventCache.invalidate_room_threads	async_method	lines 1090-1101	duplicate-found	invalidate_room_threads delegate	sqlite_event_cache.py:707
PostgresEventCache.mark_thread_stale	async_method	lines 1103-1127	related-only	mark_thread_stale pending unavailable	sqlite_event_cache.py:720
PostgresEventCache.mark_room_threads_stale	async_method	lines 1129-1151	related-only	mark_room_threads_stale pending unavailable	sqlite_event_cache.py:733
PostgresEventCache.append_event	async_method	lines 1153-1169	duplicate-found	append_event normalize delegate bool	sqlite_event_cache.py:746
PostgresEventCache.revalidate_thread_after_incremental_update	async_method	lines 1171-1189	duplicate-found	revalidate_thread_after_incremental_update delegate bool	sqlite_event_cache.py:764
PostgresEventCache.get_thread_id_for_event	async_method	lines 1191-1203	duplicate-found	get_thread_id_for_event delegate	sqlite_event_cache.py:781
PostgresEventCache.redact_event	async_method	lines 1205-1223	duplicate-found	redact_event delegate bool	sqlite_event_cache.py:796
```

## Findings

1. Backend runtime locking and lifecycle behavior is duplicated between [postgres_event_cache.py](../../src/mindroom/matrix/cache/postgres_event_cache.py:335) and [sqlite_event_cache.py](../../src/mindroom/matrix/cache/sqlite_event_cache.py:249).
Both runtimes cache per-room `asyncio.Lock` entries with active-user counts, log long waits, serialize DB operations behind a runtime DB lock plus room lock, expose disabled state, and prune idle room locks.
PostgreSQL adds reconnect state, SQLSTATE classification, advisory transaction locks, namespace logging, and durable pending invalidations, so only the lock-entry and room-lock pruning/wait logic is a safe extraction candidate.

2. Cache protocol forwarding is duplicated across [PostgresEventCache](../../src/mindroom/matrix/cache/postgres_event_cache.py:662) and [SqliteEventCache](../../src/mindroom/matrix/cache/sqlite_event_cache.py:393).
Most public methods have identical behavior shape: check disabled/default result via `_read_operation` or `_write_operation`, pass the same room/thread/event parameters to backend-specific helper modules, normalize events, group batch writes by room, compute `time.time()` timestamps, and coerce optional boolean results.
The differences to preserve are namespace injection for PostgreSQL, different connection types, PostgreSQL retry/unavailable behavior, and pending invalidation flushing before operations.

3. Schema creation is structurally duplicated between [create_postgres_event_cache_schema](../../src/mindroom/matrix/cache/postgres_event_cache.py:153) and [create_event_cache_schema](../../src/mindroom/matrix/cache/sqlite_event_cache.py:88).
The two schemas define the same logical stores: thread events, point lookup events, edit indexes, event-thread indexes, redaction tombstones, MXC text, thread state, and room state.
The PostgreSQL version adds `namespace`, `write_seq`, metadata, and PostgreSQL syntax, while SQLite uses `rowid`, `PRAGMA user_version`, and simpler primary keys.

4. Best-effort rollback and close helpers are duplicated between [postgres_event_cache.py](../../src/mindroom/matrix/cache/postgres_event_cache.py:97) and [sqlite_event_cache.py](../../src/mindroom/matrix/cache/sqlite_event_cache.py:46).
Both call the connection method, catch `Exception`, and log debug details without masking the original failure.
The only meaningful differences are logger message text and PostgreSQL namespace fields.

## Proposed Generalization

No broad refactor recommended.
A minimal safe path would be:

1. Extract a small backend-agnostic room-lock table helper from the runtime classes into `src/mindroom/matrix/cache/room_locks.py`.
2. Keep database operation acquisition in each backend runtime so PostgreSQL advisory locks and reconnect behavior remain explicit.
3. Optionally extract tiny pure timestamp/default helpers used by `store_events_batch`, `replace_thread`, `replace_thread_if_not_newer`, and `append_event`.
4. Leave schema creation and SQL helper calls backend-specific.
5. Add parity tests for the extracted lock helper before wiring both runtimes to it.

## Risk/tests

Room lock extraction risks changing serialization and pruning behavior under concurrent cache operations.
Tests should cover lock active-user accounting, LRU pruning of idle locks, no pruning of active locks, and wait logging thresholds with a controlled clock or patched `time.perf_counter`.
Forwarding-method deduplication would have higher risk because PostgreSQL flushes pending invalidations and retries transient failures while SQLite does not.
Schema deduplication is not recommended because backend-specific SQL differences are substantial and visible.
