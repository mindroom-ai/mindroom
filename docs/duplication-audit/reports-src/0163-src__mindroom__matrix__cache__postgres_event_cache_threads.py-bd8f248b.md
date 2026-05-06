## Summary

Top duplication candidates for `src/mindroom/matrix/cache/postgres_event_cache_threads.py`:

1. The module is a near-complete PostgreSQL mirror of `src/mindroom/matrix/cache/sqlite_event_cache_threads.py`, with the same thread snapshot, freshness, invalidation, incremental append, and ID lookup behavior expressed in backend-specific SQL.
2. PostgreSQL cursor helpers `_fetchone` and `_fetchall` duplicate identical helpers in `src/mindroom/matrix/cache/postgres_event_cache_events.py`.
3. The stale-state upsert SQL in `mark_thread_stale_locked` and `mark_room_stale_locked` repeats the same "keep newest invalidation timestamp and reason" behavior with only table/key differences.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_fetchone	async_function	lines 36-45	duplicate-found	async def _fetchone; fetchone close cursor	src/mindroom/matrix/cache/postgres_event_cache_events.py:36; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:166; src/mindroom/matrix/cache/postgres_event_cache.py:302
_fetchall	async_function	lines 48-58	duplicate-found	async def _fetchall; fetchall tuple rows close cursor	src/mindroom/matrix/cache/postgres_event_cache_events.py:48; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:52; src/mindroom/matrix/cache/sqlite_event_cache_events.py:112
load_thread_events	async_function	lines 61-81	duplicate-found	load_thread_events; thread_events ORDER BY origin_server_ts	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:36; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:141
load_recent_room_thread_ids	async_function	lines 84-104	duplicate-found	load_recent_room_thread_ids; GROUP BY thread_id ORDER BY MAX(origin_server_ts)	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:59
load_thread_cache_state_row	async_function	lines 107-143	duplicate-found	load_thread_cache_state_row; thread_cache_state room_cache_state LEFT JOIN	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:82
load_thread_cache_state	async_function	lines 146-168	duplicate-found	load_thread_cache_state; ThreadCacheState row mapping	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:119; src/mindroom/matrix/cache/thread_cache_helpers.py:13
store_thread_events_locked	async_function	lines 171-233	duplicate-found	store_thread_events_locked; normalize_event_source_for_cache; write_lookup_index_rows	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:142; src/mindroom/matrix/cache/postgres_event_cache_threads.py:599; src/mindroom/matrix/cache/postgres_event_cache_events.py:418
replace_thread_locked	async_function	lines 236-281	duplicate-found	replace_thread_locked; delete_cached_events delete_event_edit_rows delete_event_thread_rows	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:221; src/mindroom/matrix/cache/postgres_event_cache_threads.py:329; src/mindroom/matrix/cache/postgres_event_cache_events.py:317
_thread_cache_state_changed_after	function	lines 284-296	duplicate-found	_thread_cache_state_changed_after; fetch_started_at validated_at invalidated_at	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:260
replace_thread_locked_if_not_newer	async_function	lines 299-326	duplicate-found	replace_thread_locked_if_not_newer; changed_after fetch_started_at	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:275
invalidate_thread_locked	async_function	lines 329-371	duplicate-found	invalidate_thread_locked; DELETE thread_events; delete lookup edit thread rows	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:302; src/mindroom/matrix/cache/postgres_event_cache_threads.py:236; src/mindroom/matrix/cache/postgres_event_cache_events.py:317
invalidate_room_threads_locked	async_function	lines 374-417	duplicate-found	invalidate_room_threads_locked; DELETE room_cache_state; room thread ids	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:339; src/mindroom/matrix/cache/postgres_event_cache_threads.py:329
mark_thread_stale_locked	async_function	lines 420-457	duplicate-found	mark_thread_stale_locked; invalidated_at CASE excluded.invalidated_at	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:382; src/mindroom/matrix/cache/postgres_event_cache_threads.py:496
revalidate_thread_after_incremental_update_locked	async_function	lines 460-493	duplicate-found	revalidate_thread_after_incremental_update_locked; _INCREMENTAL_THREAD_REVALIDATION_REASONS	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:418
mark_room_stale_locked	async_function	lines 496-525	duplicate-found	mark_room_stale_locked; room_cache_state invalidated_at CASE	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:452; src/mindroom/matrix/cache/postgres_event_cache_threads.py:420
append_existing_thread_event	async_function	lines 528-596	duplicate-found	append_existing_thread_event; event_or_original_is_redacted; SELECT 1 FROM thread_events	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:485; src/mindroom/matrix/cache/postgres_event_cache_threads.py:171
_upsert_thread_cache_state	async_function	lines 599-624	duplicate-found	upsert_thread_cache_state; validated_at invalidated_at NULL	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:151; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:202
_thread_event_ids_for_thread	async_function	lines 627-644	duplicate-found	_thread_event_ids_for_thread; SELECT event_id FROM thread_events WHERE thread_id	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:547; src/mindroom/matrix/cache/postgres_event_cache_threads.py:647
_thread_event_ids_for_room	async_function	lines 647-663	duplicate-found	_thread_event_ids_for_room; SELECT event_id FROM thread_events WHERE room_id	src/mindroom/matrix/cache/sqlite_event_cache_threads.py:567; src/mindroom/matrix/cache/postgres_event_cache_threads.py:627
```

## Findings

### 1. PostgreSQL and SQLite thread-cache modules duplicate the same behavior

`src/mindroom/matrix/cache/postgres_event_cache_threads.py` and `src/mindroom/matrix/cache/sqlite_event_cache_threads.py` implement the same thread-cache API almost symbol-for-symbol.
Examples include loading sorted thread events in PostgreSQL lines 61-81 vs SQLite lines 36-56, loading recent thread IDs in PostgreSQL lines 84-104 vs SQLite lines 59-79, loading and converting joined thread/room freshness state in PostgreSQL lines 107-168 vs SQLite lines 82-139, replacing/invalidation flows in PostgreSQL lines 236-417 vs SQLite lines 221-379, and incremental append in PostgreSQL lines 528-596 vs SQLite lines 485-544.

The duplicated behavior is real because the functions preserve the same public semantics: normalize events, filter redacted items, write thread snapshot rows, write lookup/edit/thread indexes, update freshness state, guard stale fetch replacement, and delete related lookup/index rows.
Differences to preserve are database-specific: namespace columns exist only in PostgreSQL, placeholder syntax differs, PostgreSQL uses `write_seq` for ordering and conflict updates, SQLite uses `rowid` or `INSERT OR REPLACE`, and PostgreSQL methods accept optional `invalidated_at` overrides while the SQLite counterparts currently call `time.time()` directly.

### 2. PostgreSQL cursor helper duplication

`_fetchone` in PostgreSQL thread cache lines 36-45 is identical to `src/mindroom/matrix/cache/postgres_event_cache_events.py` lines 36-45.
`_fetchall` in PostgreSQL thread cache lines 48-58 is identical to `src/mindroom/matrix/cache/postgres_event_cache_events.py` lines 48-58.
Both modules execute a literal SQL string with params, fetch one/all rows, convert all rows to tuples in `_fetchall`, and close the cursor in a `finally` block.

This is low-risk duplication but active; future fixes to psycopg cursor handling would need to be repeated.
The helper signatures use `LiteralString` and `AsyncConnection`, so they can move to a PostgreSQL cache utility module without changing caller behavior.

### 3. Thread cleanup flow repeats within the module and across event redaction

`replace_thread_locked` lines 246-273, `invalidate_thread_locked` lines 337-364, and `invalidate_room_threads_locked` lines 381-403 all collect thread event IDs, delete thread-event rows, then delete cached event rows, edit-index rows, and event-thread rows.
`src/mindroom/matrix/cache/postgres_event_cache_events.py` lines 317-358 performs the same cleanup family during redaction, with the additional dependent-edit and redaction tombstone steps.

The duplication is behavioral rather than literal: each path removes a set of event IDs from point lookup, edit index, and thread index tables after thread rows become invalid.
Differences to preserve are scope and side effects: thread replacement invalidates by thread or room and does not record redaction tombstones, while redaction deletes only specified room events and does record tombstones.

### 4. Stale-state upsert SQL repeats the newest-invalidation rule

`mark_thread_stale_locked` lines 420-457 and `mark_room_stale_locked` lines 496-525 both compute a stale timestamp and upsert a cache-state row.
Both keep the existing invalidation timestamp and reason when the incoming timestamp is older, and replace both when the incoming timestamp is newer or the stored timestamp is absent.
The SQLite equivalents repeat the same rule in `src/mindroom/matrix/cache/sqlite_event_cache_threads.py` lines 382-415 and 452-482.

Differences to preserve are table/key shape and PostgreSQL's optional `invalidated_at` parameter.
The comparison rule itself is a shared behavior.

### 5. Thread-event insertion and lookup-index writing repeats between full-store and append

`store_thread_events_locked` lines 191-226 and `append_existing_thread_event` lines 547-595 both serialize cacheable events, insert or update `mindroom_event_cache_thread_events`, and call `write_lookup_index_rows`.
The SQL insert/update block at lines 199-218 is duplicated by lines 569-587.

Differences to preserve are redaction/filtering and append semantics: full-store normalizes and filters a batch before writing state, while append checks a single already-normalized event against redaction tombstones, returns `False` when the thread snapshot does not already exist, and still writes lookup indexes for the event.

## Proposed Generalization

No broad refactor recommended in this audit because the largest duplication is the intentional two-backend SQLite/PostgreSQL split and database-specific SQL is embedded throughout.

Minimal candidates if production work is later requested:

1. Move PostgreSQL `_fetchone`, `_fetchall`, and possibly `_rowcount` into a small `postgres_helpers.py` module under `src/mindroom/matrix/cache/`.
2. Extract a tiny PostgreSQL helper for deleting lookup/edit/thread rows for a list of event IDs, used by thread replacement and invalidation, while leaving redaction tombstone behavior in `postgres_event_cache_events.py`.
3. Extract the repeated PostgreSQL `mindroom_event_cache_thread_events` upsert loop into `_write_thread_event_rows`.
4. Leave SQLite/PostgreSQL full API duplication alone unless a larger storage-interface refactor is already planned.

## Risk/tests

The main risk in deduplicating this module is changing backend-specific ordering, conflict, or namespace behavior.
Tests should cover PostgreSQL and SQLite parity for thread event ordering, stale-state precedence when older invalidations arrive after newer ones, replacement guarded by `fetch_started_at`, deleting stale lookup/edit/thread rows during thread invalidation, and append behavior when the thread snapshot is absent.
Existing tests around redaction should also be run if cleanup helpers are shared with event redaction paths.
