Summary: The primary duplication is the SQLite agent-message snapshot reader mirrored by the PostgreSQL reader in `src/mindroom/matrix/cache/postgres_agent_message_snapshot.py`.
The duplicated behavior covers scope filtering, thread-cache rejection handling, edit resolution, visible-content extraction, event scanning, and public error translation.
The storage-specific SQL, DB connection types, namespace argument, ordering column, and database exception class are the meaningful differences to preserve.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_SnapshotLookupResult	class	lines 22-26	duplicate-found	SnapshotLookupResult AgentMessageSnapshot stop_scanning	src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:22
_visible_content	function	lines 32-36	duplicate-found	visible_content_from_content content isinstance dict	src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:33; src/mindroom/approval_events.py:88
_event_matches_scope	function	lines 39-50	duplicate-found	EventInfo.from_event relation_type m.replace m.thread sender	src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:40
_thread_scope_has_no_snapshot	async_function	lines 53-74	duplicate-found	thread_cache_rejection_reason no_cache_state cache_never_validated AgentMessageSnapshotUnavailable	src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:54; src/mindroom/matrix/cache/thread_cache_helpers.py:30
_snapshot_from_event	async_function	lines 77-113	duplicate-found	load_latest_edit_row AgentMessageSnapshot origin_server_ts runtime_started_at stop_scanning	src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:80; src/mindroom/matrix/cache/sqlite_event_cache_events.py:159; src/mindroom/matrix/cache/postgres_event_cache_events.py:204
_iter_scope_events	async_function	lines 116-140	duplicate-found	SELECT event_json cached_at thread_events ORDER BY origin_server_ts	src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:121
_load_scope_snapshot	async_function	lines 143-181	duplicate-found	iter_scope_events json.loads event_matches_scope snapshot_from_event cursor.close	src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:149
load_sqlite_agent_message_snapshot	async_function	lines 184-212	duplicate-found	load_agent_message_snapshot JSONDecodeError sqlite3.Error psycopg.Error	src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:193; src/mindroom/matrix/cache/sqlite_event_cache.py:564; src/mindroom/matrix/cache/postgres_event_cache.py:939
```

## Findings

### 1. SQLite and PostgreSQL agent-message snapshot readers are near-identical

`src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:22` through `src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:212` duplicates the control flow and most pure behavior from `src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:22` through `src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:224`.
Both modules define the same `_SnapshotLookupResult` dataclass, extract replacement-aware visible content the same way, apply the same Matrix event scope predicate, reject unusable thread snapshots with the same policy, resolve latest edits before building `AgentMessageSnapshot`, scan newest-to-oldest rows until a usable snapshot is found, and translate JSON/DB errors into `AgentMessageSnapshotUnavailable`.

The differences are storage-adapter details.
PostgreSQL carries a `namespace` parameter, uses `%s` placeholders, `mindroom_event_cache_*` table names, `write_seq` as the stable tie-breaker, and catches `psycopg.Error`.
SQLite omits namespace, uses `?` placeholders, local table names, `rowid` as the tie-breaker, and catches `sqlite3.Error`.

### 2. Visible-content extraction has small related duplication

`src/mindroom/matrix/cache/sqlite_agent_message_snapshot.py:32` and `src/mindroom/matrix/cache/postgres_agent_message_snapshot.py:33` both unwrap `event["content"]`, guard that it is a dict, and call `visible_content_from_content`.
`src/mindroom/approval_events.py:88` performs a similar replacement-aware visible-content extraction for approval status, but it is embedded in domain-specific validation and returns a status fallback rather than a generic visible content mapping.

This is related behavior, but the active duplication worth addressing is still the two cache backends.

### 3. Event-cache row helpers show the same backend-pair duplication pattern

The snapshot reader depends on `load_latest_edit_row`.
That helper is duplicated across `src/mindroom/matrix/cache/sqlite_event_cache_events.py:159` and `src/mindroom/matrix/cache/postgres_event_cache_events.py:204`, returning the same `CachedEventRow` shape after storage-specific SQL.
This supports the same broader pattern: backend-specific SQL modules duplicate pure event-cache policy around small query differences.

## Proposed Generalization

Introduce a small shared policy module such as `src/mindroom/matrix/cache/agent_message_snapshot_policy.py` for pure or storage-neutral behavior:

1. Move `_SnapshotLookupResult`, `_visible_content`, `_event_matches_scope`, and the snapshot-building portion of `_snapshot_from_event` into the shared module.
2. Add a small storage protocol or callback bundle for `load_thread_cache_state`, `load_latest_edit_row`, and `iter_scope_events`, leaving SQL in the SQLite/PostgreSQL modules.
3. Keep each backend's public `load_*_agent_message_snapshot` wrapper responsible for namespace handling, SQL cursor creation, and backend-specific exception translation.
4. Add focused parity tests that exercise the shared policy once, plus one backend test each for query ordering and exception translation.

This would remove the largest duplicate block without forcing a broad event-cache architecture change.

## Risk/tests

The main behavior risk is changing subtle scan-stop semantics for room-level snapshots.
The current logic stops scanning when a room-level visible event predates `runtime_started_at` or lacks `cached_at`, but it does not apply that barrier to thread snapshots.

Tests should cover original messages, replacement edits, invalid timestamps, non-message events, sender mismatches, thread-vs-room scope filtering, missing/unvalidated/stale thread cache states, corrupt JSON, and the backend-specific DB exception paths.
No production code was edited for this audit.
