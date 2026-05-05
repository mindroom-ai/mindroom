# Summary

Top duplication candidates:

- `src/mindroom/matrix/cache/sqlite_event_cache_events.py` is a near behavior-level mirror of `src/mindroom/matrix/cache/postgres_event_cache_events.py`.
  The storage backend details differ, but event serialization, latest-edit lookup, MXC text caching, derived edit/thread indexes, redaction filtering, and redaction cleanup all implement the same domain rules.
- The serialization/filtering helpers are already reused by `src/mindroom/matrix/cache/sqlite_event_cache_threads.py`, so they are not duplicate within the SQLite backend.
  Their duplicated implementation is the Postgres copy.
- Latest-edit row selection is also consumed by the SQLite and Postgres agent-message snapshot readers, but those modules call the cache helper rather than duplicating the query logic.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SerializedCachedEvent	class	lines 18-24	duplicate-found	SerializedCachedEvent dataclass event_id origin_server_ts event_json event	src/mindroom/matrix/cache/postgres_event_cache_events.py:18
CachedEventRow	class	lines 28-32	duplicate-found	CachedEventRow dataclass event cached_at latest edit row	src/mindroom/matrix/cache/postgres_event_cache_events.py:28
event_id_for_cache	function	lines 35-41	duplicate-found	event_id_for_cache missing event_id cached Matrix event	src/mindroom/matrix/cache/postgres_event_cache_events.py:73; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:15
event_timestamp_for_cache	function	lines 44-50	duplicate-found	event_timestamp_for_cache origin_server_ts bool validation	src/mindroom/matrix/cache/postgres_event_cache_events.py:82; src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:104; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:109
serialize_cached_event	function	lines 53-60	duplicate-found	serialize_cached_event json.dumps separators origin_server_ts	src/mindroom/matrix/cache/postgres_event_cache_events.py:91; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:19; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:502
serialize_cacheable_events	function	lines 63-67	duplicate-found	serialize_cacheable_events batch serialize cached events	src/mindroom/matrix/cache/postgres_event_cache_events.py:101; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:177
load_event	async_function	lines 70-86	duplicate-found	load_event SELECT event_json event_id json.loads	src/mindroom/matrix/cache/postgres_event_cache_events.py:108
load_recent_room_events	async_function	lines 89-114	duplicate-found	load_recent_room_events room_id event_type since_ts_ms limit newest first	src/mindroom/matrix/cache/postgres_event_cache_events.py:127
load_latest_edit	async_function	lines 117-156	duplicate-found	load_latest_edit event_edits join events sender original_event_id	src/mindroom/matrix/cache/postgres_event_cache_events.py:156; src/mindroom/approval_manager.py:846
load_latest_edit_row	async_function	lines 159-184	duplicate-found	load_latest_edit_row event_json cached_at latest edit	src/mindroom/matrix/cache/postgres_event_cache_events.py:204; src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:90; src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:94
load_mxc_text	async_function	lines 187-203	duplicate-found	load_mxc_text mxc_text_cache text_content mxc_url	src/mindroom/matrix/cache/postgres_event_cache_events.py:236; src/mindroom/matrix/message_content.py:120
persist_mxc_text	async_function	lines 206-220	duplicate-found	persist_mxc_text mxc_text_cache text_content cached_at src/mindroom/matrix/cache/postgres_event_cache_events.py:255; src/mindroom/matrix/message_content.py:178
persist_lookup_events	async_function	lines 223-239	duplicate-found	persist_lookup_events filter_cacheable_events write_lookup_index_rows	src/mindroom/matrix/cache/postgres_event_cache_events.py:276
load_thread_id_for_event	async_function	lines 242-259	duplicate-found	load_thread_id_for_event event_threads thread_id event_id	src/mindroom/matrix/cache/postgres_event_cache_events.py:297
redact_event_locked	async_function	lines 262-289	duplicate-found	redact_event_locked dependent edits delete cached events record redacted	src/mindroom/matrix/cache/postgres_event_cache_events.py:317; src/mindroom/matrix/cache/thread_write_cache_ops.py:143; src/mindroom/matrix/cache/thread_writes.py:226
event_or_original_is_redacted	async_function	lines 292-310	duplicate-found	event_or_original_is_redacted EventInfo original_event_id redacted candidates	src/mindroom/matrix/cache/postgres_event_cache_events.py:361; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:493
filter_cacheable_events	async_function	lines 313-342	duplicate-found	filter_cacheable_events redacted original_event_id candidate_ids	src/mindroom/matrix/cache/postgres_event_cache_events.py:384; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:172
write_lookup_index_rows	async_function	lines 345-403	duplicate-found	write_lookup_index_rows events event_edits event_threads thread root self rows	src/mindroom/matrix/cache/postgres_event_cache_events.py:418; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:195; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:537
dependent_edit_event_ids	async_function	lines 406-423	duplicate-found	dependent_edit_event_ids edit_event_id original_event_id src/mindroom/matrix/cache/postgres_event_cache_events.py:492
delete_cached_events	async_function	lines 426-441	duplicate-found	delete_cached_events DELETE events event_ids rowcount	src/mindroom/matrix/cache/postgres_event_cache_events.py:512; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:239
delete_event_thread_rows	async_function	lines 444-460	duplicate-found	delete_event_thread_rows DELETE event_threads event_ids rowcount	src/mindroom/matrix/cache/postgres_event_cache_events.py:531; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:246
delete_event_edit_rows	async_function	lines 463-492	duplicate-found	delete_event_edit_rows edit_event_id original_event_id deleted_rows	src/mindroom/matrix/cache/postgres_event_cache_events.py:551; src/mindroom/matrix/cache/sqlite_event_cache_threads.py:240
_event_thread_row	function	lines 495-506	duplicate-found	_event_thread_row EventInfo thread_id thread_id_from_edit	src/mindroom/matrix/cache/postgres_event_cache_events.py:582
_with_thread_root_self_rows	function	lines 509-522	duplicate-found	_with_thread_root_self_rows dict.fromkeys thread root self row	src/mindroom/matrix/cache/postgres_event_cache_events.py:600
_edit_cache_row	function	lines 525-539	duplicate-found	_edit_cache_row editable event types m.replace original_event_id	src/mindroom/matrix/cache/postgres_event_cache_events.py:619
_delete_room_thread_events	async_function	lines 542-558	duplicate-found	_delete_room_thread_events DELETE thread_events event_ids rowcount	src/mindroom/matrix/cache/postgres_event_cache_events.py:637
_record_redacted_events	async_function	lines 561-576	duplicate-found	_record_redacted_events redacted_events tombstones insert src/mindroom/matrix/cache/postgres_event_cache_events.py:657
_redacted_event_ids_for_candidates	async_function	lines 579-600	duplicate-found	_redacted_event_ids_for_candidates redacted_events event_id IN ANY	src/mindroom/matrix/cache/postgres_event_cache_events.py:676
```

# Findings

1. SQLite and Postgres event-cache event modules duplicate the same domain behavior.

   - SQLite: `src/mindroom/matrix/cache/sqlite_event_cache_events.py:17-600`.
   - Postgres: `src/mindroom/matrix/cache/postgres_event_cache_events.py:18-695`.
   - Both define the same event row dataclasses, event ID/timestamp validation, compact JSON serialization, event lookup reads, latest edit reads, MXC text cache reads/writes, point lookup persistence, edit/thread index maintenance, redaction filtering, dependent edit deletion, and durable redaction tombstones.
   - Differences to preserve: Postgres carries a `namespace`, uses `%s` placeholders and `ANY(%s)`, has helper cursor wrappers, uses `write_seq` instead of SQLite `rowid`, and writes some batches as loops because of psycopg parameter behavior.

2. Thread-index row derivation is duplicated across backends.

   - SQLite: `_event_thread_row`, `_with_thread_root_self_rows`, and `_edit_cache_row` at `src/mindroom/matrix/cache/sqlite_event_cache_events.py:495`, `src/mindroom/matrix/cache/sqlite_event_cache_events.py:509`, and `src/mindroom/matrix/cache/sqlite_event_cache_events.py:525`.
   - Postgres: matching helpers at `src/mindroom/matrix/cache/postgres_event_cache_events.py:582`, `src/mindroom/matrix/cache/postgres_event_cache_events.py:600`, and `src/mindroom/matrix/cache/postgres_event_cache_events.py:619`.
   - The same `EventInfo` decisions are repeated: derive explicit thread membership from `thread_id` or `thread_id_from_edit`, add thread-root self rows, and index editable replacement events.
   - Differences to preserve: tuple shape includes `namespace` in Postgres but not SQLite.

3. Redaction filtering and cleanup rules are duplicated across backends.

   - SQLite: `event_or_original_is_redacted`, `filter_cacheable_events`, `redact_event_locked`, `_record_redacted_events`, and `_redacted_event_ids_for_candidates` at `src/mindroom/matrix/cache/sqlite_event_cache_events.py:292`, `src/mindroom/matrix/cache/sqlite_event_cache_events.py:313`, `src/mindroom/matrix/cache/sqlite_event_cache_events.py:262`, `src/mindroom/matrix/cache/sqlite_event_cache_events.py:561`, and `src/mindroom/matrix/cache/sqlite_event_cache_events.py:579`.
   - Postgres: matching behavior at `src/mindroom/matrix/cache/postgres_event_cache_events.py:361`, `src/mindroom/matrix/cache/postgres_event_cache_events.py:384`, `src/mindroom/matrix/cache/postgres_event_cache_events.py:317`, `src/mindroom/matrix/cache/postgres_event_cache_events.py:657`, and `src/mindroom/matrix/cache/postgres_event_cache_events.py:676`.
   - Both backends tombstone redacted event IDs durably, reject re-caching redacted originals and edits, delete dependent edits when an original is redacted, and report whether any cached rows were removed.
   - Differences to preserve: namespace scoping and backend-specific delete batching.

# Proposed Generalization

Minimal refactor plan:

1. Extract backend-independent pure event helpers into a shared module such as `src/mindroom/matrix/cache/event_cache_rows.py`.
2. Move `SerializedCachedEvent`, `CachedEventRow`, `_EDITABLE_EVENT_TYPES`, `event_id_for_cache`, `event_timestamp_for_cache`, `serialize_cached_event`, `serialize_cacheable_events`, and the pure row-derivation logic there.
3. Represent derived edit/thread rows with small dataclasses or backend-neutral tuples, then let SQLite/Postgres adapters add namespace/table-specific SQL parameters.
4. Keep SQL reads/writes in the existing backend modules; only share pure transformation and redaction candidate selection logic.
5. Add parity tests that run the pure helpers once and backend integration tests that assert SQLite and Postgres return equivalent visible behavior for edits, thread mappings, MXC text, and redactions.

# Risk/tests

Risk is moderate because this file is persistence code and backend ordering semantics matter.
The main risks are accidentally changing latest-edit tie-breaking (`rowid` versus `write_seq`), losing namespace scoping in Postgres, and changing redaction tombstone behavior for edits of redacted originals.

Tests needing attention:

- Unit tests for event ID/timestamp validation and compact serialization.
- Unit tests for edit row and thread row derivation, including edits that carry thread metadata.
- Backend parity tests for `load_latest_edit`, `load_latest_edit_row`, `filter_cacheable_events`, and `redact_event_locked`.
- Existing SQLite/Postgres event-cache tests around thread snapshots and agent-message snapshots.
