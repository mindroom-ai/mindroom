## Summary

Top duplication candidates:

1. `src/mindroom/matrix/cache/postgres_event_cache_events.py` and `src/mindroom/matrix/cache/sqlite_event_cache_events.py` duplicate the same Matrix event-cache behavior across PostgreSQL and SQLite backends, with mostly dialect-specific SQL and namespace differences.
2. The pure event shaping helpers for event IDs, timestamps, serialization, edit rows, thread rows, redaction candidate collection, and thread-root self rows are backend-independent and are near-identical between the two event-cache modules.
3. PostgreSQL cursor helper behavior (`_fetchone`, `_fetchall`) is duplicated between `postgres_event_cache_events.py` and `postgres_event_cache_threads.py`.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SerializedCachedEvent	class	lines 19-25	duplicate-found	SerializedCachedEvent dataclass event_id origin_server_ts event_json event	src/mindroom/matrix/cache/sqlite_event_cache_events.py:18
CachedEventRow	class	lines 29-33	duplicate-found	CachedEventRow dataclass event cached_at	src/mindroom/matrix/cache/sqlite_event_cache_events.py:28
_fetchone	async_function	lines 36-45	duplicate-found	async def _fetchone AsyncConnection cursor.fetchone close	src/mindroom/matrix/cache/postgres_event_cache_threads.py:36
_fetchall	async_function	lines 48-58	duplicate-found	async def _fetchall AsyncConnection cursor.fetchall tuple rows close	src/mindroom/matrix/cache/postgres_event_cache_threads.py:48
_rowcount	async_function	lines 61-70	related-only	rowcount is None int cursor.rowcount delete rowcount	src/mindroom/matrix/cache/sqlite_event_cache_events.py:441, src/mindroom/matrix/cache/sqlite_event_cache_events.py:460, src/mindroom/matrix/cache/sqlite_event_cache_events.py:480, src/mindroom/matrix/cache/sqlite_event_cache_events.py:490, src/mindroom/matrix/cache/sqlite_event_cache_events.py:558
event_id_for_cache	function	lines 73-79	duplicate-found	event_id_for_cache missing event_id Cached Matrix event	src/mindroom/matrix/cache/sqlite_event_cache_events.py:35
event_timestamp_for_cache	function	lines 82-88	duplicate-found	event_timestamp_for_cache origin_server_ts bool missing	src/mindroom/matrix/cache/sqlite_event_cache_events.py:44
serialize_cached_event	function	lines 91-98	duplicate-found	serialize_cached_event json.dumps separators SerializedCachedEvent	src/mindroom/matrix/cache/sqlite_event_cache_events.py:53
serialize_cacheable_events	function	lines 101-105	duplicate-found	serialize_cacheable_events list comprehension serialize_cached_event	src/mindroom/matrix/cache/sqlite_event_cache_events.py:63
load_event	async_function	lines 108-124	duplicate-found	load_event SELECT event_json WHERE event_id json.loads	src/mindroom/matrix/cache/sqlite_event_cache_events.py:70
load_recent_room_events	async_function	lines 127-153	duplicate-found	load_recent_room_events event_type since_ts_ms limit ORDER BY origin_server_ts	src/mindroom/matrix/cache/sqlite_event_cache_events.py:89
load_latest_edit	async_function	lines 156-201	duplicate-found	load_latest_edit event_edits original_event_id sender latest edit	src/mindroom/matrix/cache/sqlite_event_cache_events.py:117
load_latest_edit_row	async_function	lines 204-233	duplicate-found	load_latest_edit_row CachedEventRow latest edit cached_at	src/mindroom/matrix/cache/sqlite_event_cache_events.py:159
load_mxc_text	async_function	lines 236-252	duplicate-found	load_mxc_text mxc_text_cache text_content mxc_url	src/mindroom/matrix/cache/sqlite_event_cache_events.py:187
persist_mxc_text	async_function	lines 255-273	duplicate-found	persist_mxc_text mxc_text_cache text_content cached_at	src/mindroom/matrix/cache/sqlite_event_cache_events.py:206
persist_lookup_events	async_function	lines 276-294	duplicate-found	persist_lookup_events filter_cacheable_events write_lookup_index_rows serialize_cacheable_events	src/mindroom/matrix/cache/sqlite_event_cache_events.py:223
load_thread_id_for_event	async_function	lines 297-314	duplicate-found	load_thread_id_for_event event_threads thread_id event_id	src/mindroom/matrix/cache/sqlite_event_cache_events.py:242
redact_event_locked	async_function	lines 317-358	duplicate-found	redact_event_locked dependent_edit_ids delete_cached_events record_redacted	src/mindroom/matrix/cache/sqlite_event_cache_events.py:262
event_or_original_is_redacted	async_function	lines 361-381	duplicate-found	event_or_original_is_redacted EventInfo original_event_id candidate_ids	src/mindroom/matrix/cache/sqlite_event_cache_events.py:292
filter_cacheable_events	async_function	lines 384-415	duplicate-found	filter_cacheable_events redacted_event_ids original_event_id EventInfo	src/mindroom/matrix/cache/sqlite_event_cache_events.py:313
write_lookup_index_rows	async_function	lines 418-489	duplicate-found	write_lookup_index_rows events event_edits event_threads serialized_events	src/mindroom/matrix/cache/sqlite_event_cache_events.py:345
dependent_edit_event_ids	async_function	lines 492-509	duplicate-found	dependent_edit_event_ids SELECT edit_event_id original_event_id	src/mindroom/matrix/cache/sqlite_event_cache_events.py:406
delete_cached_events	async_function	lines 512-528	duplicate-found	delete_cached_events DELETE events event_ids	src/mindroom/matrix/cache/sqlite_event_cache_events.py:426
delete_event_thread_rows	async_function	lines 531-548	duplicate-found	delete_event_thread_rows DELETE event_threads event_ids	src/mindroom/matrix/cache/sqlite_event_cache_events.py:444
delete_event_edit_rows	async_function	lines 551-579	duplicate-found	delete_event_edit_rows DELETE event_edits edit_event_id original_event_id	src/mindroom/matrix/cache/sqlite_event_cache_events.py:463
_event_thread_row	function	lines 582-597	duplicate-found	_event_thread_row EventInfo thread_id thread_id_from_edit	src/mindroom/matrix/cache/sqlite_event_cache_events.py:495
_with_thread_root_self_rows	function	lines 600-616	duplicate-found	_with_thread_root_self_rows dict.fromkeys thread_id self row	src/mindroom/matrix/cache/sqlite_event_cache_events.py:509
_edit_cache_row	function	lines 619-634	duplicate-found	_edit_cache_row EDITABLE_EVENT_TYPES is_edit original_event_id	src/mindroom/matrix/cache/sqlite_event_cache_events.py:525
_delete_room_thread_events	async_function	lines 637-654	duplicate-found	_delete_room_thread_events DELETE thread_events event_ids	src/mindroom/matrix/cache/sqlite_event_cache_events.py:542
_record_redacted_events	async_function	lines 657-673	duplicate-found	_record_redacted_events INSERT redacted_events event_ids	src/mindroom/matrix/cache/sqlite_event_cache_events.py:561
_redacted_event_ids_for_candidates	async_function	lines 676-695	duplicate-found	_redacted_event_ids_for_candidates SELECT redacted_events event_id candidates	src/mindroom/matrix/cache/sqlite_event_cache_events.py:579
```

## Findings

### 1. Backend event-cache modules duplicate most behavior

`src/mindroom/matrix/cache/postgres_event_cache_events.py:108` through `src/mindroom/matrix/cache/postgres_event_cache_events.py:695` mirrors `src/mindroom/matrix/cache/sqlite_event_cache_events.py:70` through `src/mindroom/matrix/cache/sqlite_event_cache_events.py:600`.
The duplicated behaviors include point event lookup, recent room-event lookup, latest edit lookup, MXC text cache load/store, lookup-index persistence, thread lookup, redaction tombstone checks, cache filtering, edit dependency lookup, and deletion of derived rows.

The intent is the same: maintain durable Matrix event lookup, edit, thread, MXC text, and redaction indexes.
The differences to preserve are real SQL dialect and schema details:

- PostgreSQL includes `namespace` in keys and table names, uses `%s` placeholders, `ANY(%s)`, JSONB extraction, explicit `write_seq`, and `ON CONFLICT ... DO UPDATE`.
- SQLite omits namespace, uses `?` placeholders, `json_extract`, `rowid` ordering, `executemany`, and `INSERT OR REPLACE`.

### 2. Backend-independent event shaping is duplicated verbatim

The dataclasses and pure helpers are duplicated with only docstring or tuple-width differences:

- `SerializedCachedEvent`: `postgres_event_cache_events.py:19` and `sqlite_event_cache_events.py:18`.
- `CachedEventRow`: `postgres_event_cache_events.py:29` and `sqlite_event_cache_events.py:28`.
- `event_id_for_cache`: `postgres_event_cache_events.py:73` and `sqlite_event_cache_events.py:35`.
- `event_timestamp_for_cache`: `postgres_event_cache_events.py:82` and `sqlite_event_cache_events.py:44`.
- `serialize_cached_event`: `postgres_event_cache_events.py:91` and `sqlite_event_cache_events.py:53`.
- `serialize_cacheable_events`: `postgres_event_cache_events.py:101` and `sqlite_event_cache_events.py:63`.

These are functionally identical and do not depend on database backend.

The redaction and event-info transformations are also duplicated:

- `event_or_original_is_redacted`: `postgres_event_cache_events.py:361` and `sqlite_event_cache_events.py:292`.
- `filter_cacheable_events`: `postgres_event_cache_events.py:384` and `sqlite_event_cache_events.py:313`.
- `_event_thread_row`: `postgres_event_cache_events.py:582` and `sqlite_event_cache_events.py:495`.
- `_with_thread_root_self_rows`: `postgres_event_cache_events.py:600` and `sqlite_event_cache_events.py:509`.
- `_edit_cache_row`: `postgres_event_cache_events.py:619` and `sqlite_event_cache_events.py:525`.

The main difference to preserve is tuple shape: PostgreSQL rows include `namespace`; SQLite rows do not.

### 3. PostgreSQL cursor helpers are duplicated across PostgreSQL cache modules

`_fetchone` and `_fetchall` in `postgres_event_cache_events.py:36` and `postgres_event_cache_events.py:48` are duplicated in `postgres_event_cache_threads.py:36` and `postgres_event_cache_threads.py:48`.
Both execute a query, fetch rows, normalize returned rows to tuples for `_fetchall`, and close the cursor in `finally`.

`_rowcount` has no exact PostgreSQL sibling, but the same `0 if cursor.rowcount is None else int(cursor.rowcount)` normalization appears repeatedly in the SQLite event-cache module at `sqlite_event_cache_events.py:441`, `sqlite_event_cache_events.py:460`, `sqlite_event_cache_events.py:480`, `sqlite_event_cache_events.py:490`, and `sqlite_event_cache_events.py:558`.

## Proposed Generalization

1. Move `SerializedCachedEvent`, `CachedEventRow`, `event_id_for_cache`, `event_timestamp_for_cache`, `serialize_cached_event`, and `serialize_cacheable_events` into a shared module such as `src/mindroom/matrix/cache/event_cache_rows.py`.
2. Move pure row derivation into shared helpers with backend-specific row-prefixing at the call site, for example `event_thread_identity(event) -> tuple[str, str] | None`, `with_thread_root_self_pairs`, and `edit_cache_identity(event) -> tuple[str, str, int] | None`.
3. Extract redaction candidate construction/filtering into a pure helper that accepts already-loaded redacted IDs, leaving database lookup in each backend module.
4. Optionally extract PostgreSQL `_fetchone` and `_fetchall` into a small `postgres_helpers.py` used by PostgreSQL event and thread cache modules.
5. Avoid merging full SQLite/PostgreSQL SQL functions unless a repository-wide storage abstraction is planned; SQL dialect differences are substantial enough that a generic query layer would add risk.

## Risk/tests

The safest immediate refactor is limited to pure helper extraction and import rewiring.
Tests should cover event ID/timestamp validation, JSON serialization stability, edit-row derivation for editable and non-editable events, thread-row derivation from direct thread IDs and edit-derived thread IDs, thread-root self-row deduplication, and redaction filtering of both directly redacted events and edits targeting redacted originals.

If PostgreSQL cursor helpers are extracted, existing PostgreSQL cache tests should verify cursor closure on fetch success and failure.
If shared redaction filtering is extracted, run both SQLite and PostgreSQL event-cache test suites because namespace handling and candidate lookup must remain backend-specific.
