# Duplication Audit Report

## Summary

The primary duplication is the full SQLite thread-cache backend mirrored by `src/mindroom/matrix/cache/postgres_event_cache_threads.py`.
The two modules implement the same thread snapshot, invalidation, stale marker, append, and revalidation behavior with backend-specific SQL syntax, namespace handling, and write-order columns.
There is also small intra-module duplication in `store_thread_events_locked`, where the same thread-state upsert SQL appears in both the empty-events and non-empty-events paths.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
load_thread_events	async_function	lines 36-56	duplicate-found	load_thread_events SELECT event_json thread_events ORDER BY origin_server_ts	src/mindroom/matrix/cache/postgres_event_cache_threads.py:61, src/mindroom/matrix/cache/sqlite_event_cache_events.py:70, src/mindroom/matrix/cache/postgres_event_cache_events.py:110
load_recent_room_thread_ids	async_function	lines 59-79	duplicate-found	load_recent_room_thread_ids GROUP BY thread_id MAX(origin_server_ts)	src/mindroom/matrix/cache/postgres_event_cache_threads.py:84
load_thread_cache_state_row	async_function	lines 82-116	duplicate-found	load_thread_cache_state_row thread_cache_state room_cache_state LEFT JOIN	src/mindroom/matrix/cache/postgres_event_cache_threads.py:107
load_thread_cache_state	async_function	lines 119-139	duplicate-found	load_thread_cache_state ThreadCacheState row validated invalidated	src/mindroom/matrix/cache/postgres_event_cache_threads.py:146
store_thread_events_locked	async_function	lines 142-218	duplicate-found	store_thread_events_locked normalize filter serialize write_lookup_index_rows upsert state	src/mindroom/matrix/cache/postgres_event_cache_threads.py:171, src/mindroom/matrix/cache/sqlite_event_cache_threads.py:151, src/mindroom/matrix/cache/sqlite_event_cache_threads.py:202
replace_thread_locked	async_function	lines 221-257	duplicate-found	replace_thread_locked delete thread_events delete_cached_events delete_event_edit_rows delete_event_thread_rows	src/mindroom/matrix/cache/postgres_event_cache_threads.py:236, src/mindroom/matrix/cache/sqlite_event_cache_threads.py:302
_thread_cache_state_changed_after	function	lines 260-272	duplicate-found	_thread_cache_state_changed_after validated_at invalidated_at room_invalidated_at fetch_started_at	src/mindroom/matrix/cache/postgres_event_cache_threads.py:284
replace_thread_locked_if_not_newer	async_function	lines 275-299	duplicate-found	replace_thread_locked_if_not_newer load state changed_after fetch_started_at	src/mindroom/matrix/cache/postgres_event_cache_threads.py:299
invalidate_thread_locked	async_function	lines 302-336	duplicate-found	invalidate_thread_locked delete cached events edit rows thread rows cache state	src/mindroom/matrix/cache/postgres_event_cache_threads.py:329, src/mindroom/matrix/cache/sqlite_event_cache_threads.py:221
invalidate_room_threads_locked	async_function	lines 339-379	duplicate-found	invalidate_room_threads_locked delete room thread events cache state room state	src/mindroom/matrix/cache/postgres_event_cache_threads.py:374
mark_thread_stale_locked	async_function	lines 382-415	duplicate-found	mark_thread_stale_locked invalidated_at CASE invalidation_reason ON CONFLICT	src/mindroom/matrix/cache/postgres_event_cache_threads.py:420, src/mindroom/matrix/cache/sqlite_event_cache_threads.py:452
revalidate_thread_after_incremental_update_locked	async_function	lines 418-449	duplicate-found	revalidate_thread_after_incremental_update_locked incremental reasons room_invalidated_at validated_at	src/mindroom/matrix/cache/postgres_event_cache_threads.py:460, src/mindroom/matrix/cache/thread_cache_helpers.py:30
mark_room_stale_locked	async_function	lines 452-482	duplicate-found	mark_room_stale_locked room_cache_state invalidated_at CASE invalidation_reason	src/mindroom/matrix/cache/postgres_event_cache_threads.py:496, src/mindroom/matrix/cache/sqlite_event_cache_threads.py:382
append_existing_thread_event	async_function	lines 485-544	duplicate-found	append_existing_thread_event event_or_original_is_redacted serialize_cached_event SELECT 1 write_lookup_index_rows	src/mindroom/matrix/cache/postgres_event_cache_threads.py:528
_thread_event_ids_for_thread	async_function	lines 547-564	duplicate-found	_thread_event_ids_for_thread SELECT event_id thread_id	src/mindroom/matrix/cache/postgres_event_cache_threads.py:627
_thread_event_ids_for_room	async_function	lines 567-583	duplicate-found	_thread_event_ids_for_room SELECT event_id room_id	src/mindroom/matrix/cache/postgres_event_cache_threads.py:647
```

## Findings

### 1. SQLite and Postgres thread-cache helpers duplicate the same behavior

`src/mindroom/matrix/cache/sqlite_event_cache_threads.py:36` through `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:583` mirrors `src/mindroom/matrix/cache/postgres_event_cache_threads.py:61` through `src/mindroom/matrix/cache/postgres_event_cache_threads.py:663`.
The duplicated behavior covers loading cached thread events, loading recent thread IDs, mapping raw state rows into `ThreadCacheState`, replacing snapshots, invalidating one thread or a whole room, marking stale state, conditional replacement after a fetch race check, appending a single event to an existing cached thread, and listing cached event IDs.

The duplication is functional rather than only textual.
Both backends normalize incoming events, filter redacted events, serialize events, write lookup/edit/thread indexes, remove stale lookup/index rows when replacing or invalidating snapshots, and apply the same timestamp freshness rules.
The main differences to preserve are SQL dialect, Postgres `namespace` scoping, Postgres `write_seq` ordering, placeholder syntax, Postgres per-row upserts instead of SQLite `executemany`, and backend-specific imported event helpers.

### 2. Thread-cache state freshness policy is duplicated as pure Python

`_thread_cache_state_changed_after` in `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:260` is identical in behavior to `src/mindroom/matrix/cache/postgres_event_cache_threads.py:284`.
`revalidate_thread_after_incremental_update_locked` in `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:418` also duplicates the same boolean policy as `src/mindroom/matrix/cache/postgres_event_cache_threads.py:460`.
This is a good candidate for extraction because the policy does not depend on SQL.

The nearby `src/mindroom/matrix/cache/thread_cache_helpers.py:30` already holds pure cache-state policy helpers.
It is related but not a direct duplicate: it decides whether a loaded snapshot is usable, while the audited functions decide whether a fetch result lost a race and whether an incremental stale marker may be cleared.

### 3. SQLite thread-state upsert SQL is repeated inside `store_thread_events_locked`

`src/mindroom/matrix/cache/sqlite_event_cache_threads.py:151` and `src/mindroom/matrix/cache/sqlite_event_cache_threads.py:202` both perform the same `thread_cache_state` upsert: set `validated_at`, clear `invalidated_at`, and clear `invalidation_reason`.
The Postgres backend already factors the same operation into `_upsert_thread_cache_state` at `src/mindroom/matrix/cache/postgres_event_cache_threads.py:599`.

This is small but active duplication.
The two SQLite branches must preserve the early return for empty event batches and the post-write state update for non-empty batches.

## Proposed Generalization

1. Add pure helpers to `src/mindroom/matrix/cache/thread_cache_helpers.py` for race detection and incremental revalidation eligibility.
2. Replace the SQLite and Postgres `_thread_cache_state_changed_after` implementations with calls to the shared helper.
3. Replace the duplicated `can_revalidate` expression in both backend modules with the shared helper, keeping the backend-specific SQL update in each module.
4. Add a private SQLite `_upsert_thread_cache_state` helper analogous to the Postgres helper to remove the repeated SQL inside `store_thread_events_locked`.
5. Do not merge the full SQLite and Postgres modules unless a backend abstraction already exists for SQL execution; the dialect and namespace differences make a broad refactor higher risk than the duplication justifies.

## Risk/Tests

The pure-helper extraction is low risk if covered with direct unit tests for `None`, old validation timestamps, newer thread invalidation, newer room invalidation, allowed incremental reasons, and disallowed invalidation reasons.
The SQLite upsert extraction should be covered by existing thread-cache tests for empty snapshots and non-empty replacements.
Any change touching both backends should run the SQLite cache tests and the Postgres cache tests, especially replacement, redaction, stale-marker, and incremental thread-update cases.
