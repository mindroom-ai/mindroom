## Summary

The PostgreSQL agent-message snapshot reader duplicates the SQLite agent-message snapshot reader almost entirely.
The duplicated behavior covers visible content extraction, Matrix event scope filtering, thread-cache rejection handling, latest-edit resolution, ordered scope scanning, and public snapshot load/error wrapping.
Only backend-specific details differ: namespace plumbing, SQL/table names, placeholder syntax, ordering tie-breakers, cursor type, and database exception class.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_SnapshotLookupResult	class	lines 23-27	duplicate-found	class _SnapshotLookupResult snapshot stop_scanning	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:22; src/mindroom/matrix/cache/agent_message_snapshot.py:10
_visible_content	function	lines 33-37	duplicate-found	def _visible_content visible_content_from_content content dict	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:32; src/mindroom/matrix/visible_body.py:41
_event_matches_scope	function	lines 40-51	duplicate-found	def _event_matches_scope m.room.message sender relation_type m.replace m.thread	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:39; src/mindroom/matrix/event_info.py:1
_thread_scope_has_no_snapshot	async_function	lines 54-77	duplicate-found	def _thread_scope_has_no_snapshot thread_cache_rejection_reason no_cache_state cache_never_validated	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:53; src/mindroom/matrix/cache/thread_cache_helpers.py:30
_snapshot_from_event	async_function	lines 80-118	duplicate-found	def _snapshot_from_event load_latest_edit_row runtime_started_at AgentMessageSnapshot origin_server_ts	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:77; src/mindroom/matrix/cache/postgres_event_cache_events.py:204; src/mindroom/matrix/cache/sqlite_event_cache_events.py:159
_iter_scope_events	async_function	lines 121-146	duplicate-found	def _iter_scope_events SELECT event_json cached_at thread_events ORDER BY origin_server_ts	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:116
_load_scope_snapshot	async_function	lines 149-190	duplicate-found	def _load_scope_snapshot fetchone json.loads _event_matches_scope _snapshot_from_event cursor.close	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:143
load_postgres_agent_message_snapshot	async_function	lines 193-224	duplicate-found	load_*_agent_message_snapshot json.JSONDecodeError Failed to read Matrix event cache snapshot	src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:184; src/mindroom/matrix/cache/postgres_event_cache.py:939; src/mindroom/matrix/cache/sqlite_event_cache.py:564
```

## Findings

### 1. Backend snapshot readers duplicate the same snapshot algorithm.

`src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:23` through `src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:224` mirrors `src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:22` through `src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:212`.
Both modules define the same `_SnapshotLookupResult`, `_visible_content`, `_event_matches_scope`, `_thread_scope_has_no_snapshot`, `_snapshot_from_event`, `_load_scope_snapshot`, and public loader flow.
The shared behavior is selecting the latest non-replacement `m.room.message` from a sender, excluding thread events from room-level scans, resolving latest edits before snapshot construction, stopping stale room-level scans at `runtime_started_at`, and translating cache-read failures to `AgentMessageSnapshotUnavailable`.
Differences to preserve are the PostgreSQL namespace argument, backend-specific thread-cache loader, backend-specific latest-edit loader, backend SQL in `_iter_scope_events`, row ordering by `write_seq` versus SQLite `rowid`, and `psycopg.Error` versus `sqlite3.Error`.

### 2. Visible content and timestamp extraction duplicate existing pure helper behavior in nearby modules.

`src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:33` duplicates the wrapper shape in `src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:32` and delegates to the shared `visible_content_from_content` helper at `src/mindroom/matrix/visible_body.py:41`.
`src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:109` performs the same strict `origin_server_ts` integer check used for cache serialization at `src/mindroom/matrix/cache/postgres_event_cache_events.py:82` and `src/mindroom/matrix/cache/sqlite_event_cache_events.py:44`, but this snapshot path intentionally skips malformed rows rather than raising `ValueError`.
This is related duplication, but it is subordinate to the larger duplicated SQLite/PostgreSQL snapshot-reader algorithm.

### 3. Event cache public methods duplicate backend delegation shape.

`src/mindroom/matrix/cache/postgres_event_cache.py:939` and `src/mindroom/matrix/cache/sqlite_event_cache.py:564` both expose `get_latest_agent_message_snapshot` as the same `_read_operation` wrapper around backend-specific snapshot loaders.
This duplication is consistent with the surrounding backend cache classes and is lower impact than the snapshot module duplication itself.

## Proposed Generalization

Move backend-neutral snapshot behavior to a small shared module such as `src/mindroom/matrix/cache/agent_message_snapshot_loader.py`.
Keep `_SnapshotLookupResult`, visible-content extraction, event-scope matching, snapshot construction from an event/latest edit, and cursor scan orchestration in the shared module.
Pass a tiny backend adapter or typed callbacks for loading thread cache state, loading latest edit rows, iterating scope rows, and mapping database exceptions.
Leave PostgreSQL and SQLite modules as thin backend adapters that provide SQL, namespace handling, and exception wrapping.
No refactor should change public cache APIs or cache semantics.

## Risk/Tests

Primary risk is changing stale-room behavior, edit precedence, cursor closing, or thread-cache rejection semantics while abstracting the duplicate logic.
Tests should cover room-level scans, thread-level scans, latest edit replacement, `m.replace` exclusion, room-level `m.thread` exclusion, missing or invalid `event_id`, invalid `origin_server_ts`, stale `runtime_started_at` stop-scanning, corrupt JSON, unusable thread-cache state, and both backend exception paths.
Existing backend-specific tests should run against both SQLite and PostgreSQL readers if this is refactored.
